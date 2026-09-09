"""Real-node advisory labour; inputs live in the existing immutable cost fact."""
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from app.models.multilevel_bom import OrderBomGraph
from app.models.order_estimated_cost_snapshot import SalesOrderItemEstimatedCostSnapshot
from app.models.product import Product
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from app.services.processing_cost import estimate_standard_processing_cost, get_product_processing_profile

RULE_VERSION = "multilevel-bom-processing-v1"
PRINT_FIELDS = ("print_content", "printing_colors", "printing_plate_1_id", "printing_plate_2_id", "printing_plate_3_id")
PROFILE_FIELDS = ("id", "version", "printer_mode", "die_cut_mode", "assembly_worker_days_per_1000")


def _hash(inputs):
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _profile_values(profile):
    if profile is None:
        return dict(id=None, version=0, printer_mode="auto", die_cut_mode="auto", assembly_worker_days_per_1000=None)
    return {key: str(value) if isinstance(value := getattr(profile, key), Decimal) else value for key in PROFILE_FIELDS}


def _inputs(db, item, compiled):
    from app.services.order_estimated_cost_snapshot import _item_reference
    graph_row = db.get(OrderBomGraph, item.id)
    latest = db.scalar(select(SalesOrderItemEstimatedCostSnapshot).where(
        SalesOrderItemEstimatedCostSnapshot.sales_order_item_id == item.id,
        SalesOrderItemEstimatedCostSnapshot.order_item_reference_snapshot == _item_reference(item)).order_by(
        SalesOrderItemEstimatedCostSnapshot.snapshot_version.desc()).limit(1))
    saved = None
    if latest:
        try:
            document = json.loads(latest.breakdown_json)
            saved = document.get("standard_processing", {}).get("frozen_inputs")
        except (ValueError, TypeError, AttributeError) as error:
            raise BomPlanError("多级加工成本冻结资料格式无效") from error
    if saved is not None:
        if (not isinstance(saved, dict) or saved.get("schema") != 1 or saved.get("graph_hash") != graph_row.content_hash
                or saved.get("inputs_hash") != _hash(saved.get("nodes"))):
            raise BomPlanError("多级加工成本冻结资料校验失败")
        rows = saved["nodes"]
    else:
        rows = []
        snapshots = {s.component_product_id: s for s in compiled.snapshots}
        for node in compiled.graph.nodes:
            snapshot = snapshots[node.product_id]
            product = db.get(Product, node.product_id)
            if product is None or product.customer_id != compiled.graph.customer_id or product.version != node.version:
                raise BomPlanError("加工资料尚未冻结且产品版本已变化，不能推算历史工艺")
            rows.append(dict(product_id=node.product_id, product_version=node.version,
                bom_snapshot_id=snapshot.id, source=node.source,
                product=dict(id=node.product_id, box_category=snapshot.snapshot_component_box_category,
                    box_style=snapshot.snapshot_component_box_style,
                    production_process=snapshot.snapshot_component_production_process,
                    splice_mode=snapshot.snapshot_component_splice_mode,
                    **{key: getattr(product, key) for key in PRINT_FIELDS}),
                profile=_profile_values(get_product_processing_profile(db, node.product_id))))
    if not isinstance(rows, list) or any(not isinstance(row, dict)
            or not {"product_id", "product_version", "bom_snapshot_id", "source", "product", "profile"} <= row.keys()
            or not isinstance(row["product"], dict) or "id" not in row["product"]
            or not isinstance(row["profile"], dict) or set(row["profile"]) != set(PROFILE_FIELDS) for row in rows):
        raise BomPlanError("多级加工成本冻结资料格式无效")
    identities = {(n.product_id, n.version, n.source) for n in compiled.graph.nodes}
    snapshots = {s.component_product_id: s.id for s in compiled.snapshots}
    if (len(rows) != len(identities) or {(r["product_id"], r["product_version"], r["source"]) for r in rows} != identities
            or any(r["bom_snapshot_id"] != snapshots[r["product_id"]] or r["product"]["id"] != r["product_id"] for r in rows)):
        raise BomPlanError("多级加工成本产品身份不一致")
    # Previously unknown assembly labour can be completed by the existing
    # authorized profile editor. Resolved historical values are never replaced.
    for row in rows:
        if row["source"] == "assembled" and row["profile"]["assembly_worker_days_per_1000"] is None:
            profile = get_product_processing_profile(db, row["product_id"])
            if profile is not None and profile.assembly_worker_days_per_1000 is not None:
                row["profile"] = _profile_values(profile)
    return dict(schema=1, graph_hash=graph_row.content_hash, nodes=rows, inputs_hash=_hash(rows))


def estimate_graph_processing_cost(db, item):
    compiled = read_compiled_order_bom(db, item.id)
    inputs = _inputs(db, item, compiled)
    demand = {row.product_id: row.required_units for row in plan_bom(compiled.graph, item.quantity).products}
    names = {node.product_id: node.name for node in compiled.graph.nodes}
    results, missing = [], []
    for row in inputs["nodes"]:
        profile = dict(row["profile"])
        source = row["source"]
        if source != "manufactured":
            profile.update(printer_mode="none", die_cut_mode="none")
        if source == "purchased":
            profile["assembly_worker_days_per_1000"] = None
        detail = estimate_standard_processing_cost(db, product=SimpleNamespace(**row["product"]),
            quantity=demand[row["product_id"]], profile_override=SimpleNamespace(**profile),
            supply_mode="corrugated_production" if source == "manufactured" else "external_purchase")
        if source == "assembled" and profile["assembly_worker_days_per_1000"] is None:
            detail["missing_items"].append("组装人工定额待维护")
            detail.update(calculation_status="incomplete", estimated_processing_cost=None)
        missing.extend(f"{names[row['product_id']]}：{text}" for text in detail["missing_items"])
        results.append(dict(product_id=row["product_id"], product_name=names[row["product_id"]],
            bom_snapshot_id=row["bom_snapshot_id"], source=source, quantity=demand[row["product_id"]], processing=detail))
    complete = not missing
    total = sum((Decimal(r["processing"]["estimated_processing_cost"] or 0) for r in results), Decimal(0))
    aggregate = dict(rule_version=RULE_VERSION, calculation_status="calculated" if complete else "incomplete",
        estimated_processing_cost=str(total.quantize(Decimal("0.01"))) if complete else None,
        known_processing_subtotal=str(total.quantize(Decimal("0.01"))),
        missing_items=list(dict.fromkeys(missing)), nodes=results, frozen_inputs=inputs)
    for section, mode_key in (("printing", "printer_mode"), ("die_cut", "die_cut_mode"),
                              ("joining", "joining_mode"), ("extra_assembly", "assembly_mode")):
        modes = {r["processing"][section][mode_key] for r in results}
        aggregate[section] = {mode_key: "none" if modes == {"none"} else "multiple", "color_count": None}
    return aggregate

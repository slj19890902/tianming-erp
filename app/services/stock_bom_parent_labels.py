"""Pre-print parent kit identity labels from one frozen stock replenishment demand.

The labels describe planned bundles. Printing never assembles or receives stock.
"""
import hashlib
import json

from sqlalchemy import select

from app.models.customer import Customer
from app.models.procurement_source import ProcurementSourceLink
from app.models.product import Product
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.stock_preparation import StockPreparationJob
from app.models.production_label_print import ProductionPackagingLabelPrintJob
from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
from app.services.production_label_strategy import CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
from app.services.production_packaging_label import (
    ProductionPackagingLabelError, _customer_label_fields, template_dimensions,
)
from app.services.production_packaging_label_layout import effective_layout
from app.services.product_specification import resolved_product_specification

SOURCE = "stock_bom_parent"


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _assembly_tails(db, *, stock_order_id, parent_id, component_ids, units):
    """Only attribute an actual kit batch when every consumed child traces here."""
    from app.services.stock_preparation_disposition import assembly_rows
    from app.models.warehouse_inventory import InventoryLot
    results = []
    for row in assembly_rows(db):
        if (row.get("reversed") or row.get("recipe", {}).get("parent_id") != parent_id
                or int(row.get("sets") or 0) % units == 0):
            continue
        lot = db.get(InventoryLot, row["output_lot_id"])
        if (not lot or lot.source_ref_type != "preparation_assembly"
                or not lot.finished_detail or lot.finished_detail.product_id != parent_id):
            continue
        inputs = row.get("inputs") or []
        if {int(x.get("product_id") or 0) for x in inputs} != component_ids:
            continue
        child_lots = {int(x["lot_id"]) for x in inputs}
        jobs = {job.output_lot_id: job for job in db.scalars(select(StockPreparationJob).where(
            StockPreparationJob.output_lot_id.in_(child_lots),
            StockPreparationJob.status == "completed"))}
        if set(jobs) != child_lots:
            continue
        receipts = [db.get(IncomingReceiptItem, job.receipt_item_id) for job in jobs.values()]
        if any(not receipt or not receipt.stock_replenishment_item_id for receipt in receipts):
            continue
        source_items = [db.get(StockReplenishmentOrderItem, receipt.stock_replenishment_item_id)
            for receipt in receipts]
        if any(not item or item.replenishment_order_id != stock_order_id for item in source_items):
            continue
        results.append(dict(assembly_lot_id=lot.id, assembly_key=row["key"],
            actual_sets=int(row["sets"]), tail_quantity=int(row["sets"]) % units))
    return sorted(results, key=lambda row: row["assembly_lot_id"])


def build_package(db, stock_order_id, *, assembly_lot_id=None):
    from app.services.stock_replenishment_plan import frozen_bom_plan
    order = db.get(StockReplenishmentOrder, stock_order_id)
    if not order or order.status not in {"confirmed", "partially_stocked", "stocked"}:
        raise ProductionPackagingLabelError("补库需求不存在或未正式确认")
    candidates = [(item, frozen_bom_plan(item)) for item in order.items]
    candidates = [(item, plan) for item, plan in candidates if plan]
    if not candidates:
        raise ProductionPackagingLabelError("该补库需求没有冻结的 BOM 父件计划")
    plans_by_identity = {(p.get("parent_product_id"), p.get("version_fingerprint")) for _, p in candidates}
    if len(plans_by_identity) != 1:
        raise ProductionPackagingLabelError("同一补库需求包含多个父件或 BOM 版本，请分别核对")
    plan = candidates[0][1]
    if any(p != plan for _, p in candidates):
        raise ProductionPackagingLabelError("补库子件的冻结组套计划不一致")
    expected_components = {int(c["product_id"]) for c in plan["components"]}
    actual_components = {int(item.reference_product_id or item.product_id or 0) for item, _ in candidates}
    component_sheets = {int(c["product_id"]): int(c["sheet_quantity"]) for c in plan["components"]}
    if (not expected_components or actual_components != expected_components
            or len(candidates) != len(expected_components)
            or any(item.quantity != component_sheets.get(int(item.reference_product_id or item.product_id or 0))
                for item, _ in candidates)):
        raise ProductionPackagingLabelError("补库需求缺少冻结 BOM 子件，不能预印父件标签")
    parent = db.get(Product, plan["parent_product_id"])
    customer = db.get(Customer, plan["customer_id"])
    if (not parent or not customer or not parent.is_active or parent.deleted_at
            or parent.customer_id != customer.id or order.customer_id != customer.id
            or parent.version != plan["parent_product_version"]
            or not parent.production_label_enabled):
        raise ProductionPackagingLabelError("父件常用箱或冻结版本已变化，请核对父件标签设置")
    units = int(parent.production_label_units_per_label or 0)
    target = int(plan["finished_quantity"])
    full_count = target // units if units > 0 else 0
    if units <= 0 or target <= 0 or full_count > 10000:
        raise ProductionPackagingLabelError("父件每捆套数或计划组套数量无效")
    if full_count == 0 and assembly_lot_id is None:
        raise ProductionPackagingLabelError("计划量不足一整捆；尾捆须在实际成套数量确定后打印")
    item_ids = [item.id for item, _ in candidates]
    links = list(db.scalars(select(ProcurementSourceLink).where(
        ProcurementSourceLink.stock_replenishment_item_id.in_(item_ids),
        ProcurementSourceLink.status == "active").order_by(ProcurementSourceLink.id)))
    if not links:
        raise ProductionPackagingLabelError("子件尚未正式报料，不能预印父件标签")
    if len({link.stock_replenishment_item_id for link in links}) != len(links):
        raise ProductionPackagingLabelError("补库子件报料来源重复")
    supplier_lines = {line.id: line for line in db.scalars(select(SupplierRequisitionOrderItem).where(
        SupplierRequisitionOrderItem.id.in_([link.supplier_item_id for link in links])))}
    supplier_orders = {source.id: source for source in db.scalars(select(SupplierRequisitionOrder).where(
        SupplierRequisitionOrder.id.in_([line.supplier_order_id for line in supplier_lines.values()])))}
    for link in links:
        line = supplier_lines.get(link.supplier_item_id)
        source = supplier_orders.get(line.supplier_order_id) if line else None
        if (not line or not source or line.status != "active" or source.status != "confirmed"
                or line.requisition_qty != link.source_quantity
                or link.source_quantity != next(item.quantity for item, _ in candidates if item.id == link.stock_replenishment_item_id)):
            raise ProductionPackagingLabelError("子件报料来源已变化，请刷新父件标签")
    anchor_id = min(supplier_orders)
    template = CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    key = f"bom-parent:{order.id}:{parent.id}"
    source_evidence = [dict(stock_item_id=link.stock_replenishment_item_id,
        supplier_item_id=link.supplier_item_id, supplier_order_id=supplier_lines[link.supplier_item_id].supplier_order_id,
        link_id=link.id, source_hash=_hash(link.source_snapshot_json)) for link in links]
    label_plan = dict(selection_key=key, production_task_id=None, production_task_version=None,
        product_id=parent.id, product_version=parent.version, customer_id=customer.id,
        customer_version=customer.version, customer_name=customer.name,
        customer_code=customer.customer_code, **_customer_label_fields(template, customer.chinese_short_name),
        product_code=parent.product_code, product_name=parent.product_name,
        specification=resolved_product_specification(None, parent) or "BOM组合", order_numbers=[], customer_pos=[],
        template_version=template, total_quantity=target, units_per_label=units,
        label_count=full_count, label_quantities=[units] * full_count,
        unit_label="套", label_policy_source="product_master_current",
        quantity_basis="frozen_bom_full_bundles", stock_order_id=order.id,
        frozen_bom_fingerprint=plan["version_fingerprint"], source_evidence=source_evidence)
    tails = _assembly_tails(db, stock_order_id=order.id, parent_id=parent.id,
        component_ids=expected_components, units=units)
    chosen_tail = next((row for row in tails if row["assembly_lot_id"] == assembly_lot_id), None)
    if assembly_lot_id is not None:
        if chosen_tail is None:
            raise ProductionPackagingLabelError("该成套库存批次没有可归属本补库需求的实际尾捆")
        key = f"bom-parent-tail:{order.id}:{parent.id}:{assembly_lot_id}"
        label_plan.update(selection_key=key, quantity_basis="actual_assembled_tail",
            total_quantity=chosen_tail["actual_sets"], label_count=1,
            label_quantities=[chosen_tail["tail_quantity"]],
            assembly_lot_id=assembly_lot_id, assembly_evidence=chosen_tail)
    labels = [dict(selection_key=key, production_task_id=None, production_task_version=None,
        template_version=template, customer_id=customer.id, customer_name=customer.name,
        customer_code=customer.customer_code, customer_short_name=label_plan["customer_short_name"],
        product_code=parent.product_code, product_name=parent.product_name,
        specification=label_plan["specification"], order_numbers=[], customer_pos=[],
        quantity=label_plan["label_quantities"][i-1], total_quantity=label_plan["total_quantity"],
        unit_label="套", units_per_label=units,
        label_number=i, label_count=label_plan["label_count"]) for i in range(1, label_plan["label_count"] + 1)]
    layout = effective_layout(db)
    fingerprint = _hash(dict(stock_order_id=order.id, plan=label_plan, label_layout=layout))
    latest = None
    from app.services.production_label_operations import _load_job_package, ProductionLabelOperationError
    for job in db.scalars(select(ProductionPackagingLabelPrintJob).where(
            ProductionPackagingLabelPrintJob.supplier_order_id == anchor_id,
            ProductionPackagingLabelPrintJob.status == "printed")
            .order_by(ProductionPackagingLabelPrintJob.id.desc()).limit(200)):
        try:
            frozen = _load_job_package(job, db)
        except ProductionLabelOperationError:
            continue
        if (frozen.get("source_type") == SOURCE and frozen.get("stock_order_id") == order.id
                and frozen.get("assembly_lot_id") == assembly_lot_id):
            latest = dict(id=job.id, template_version=job.template_version,
                printed_at=job.printed_at.isoformat() if job.printed_at else None)
            break
    return dict(source_type=SOURCE, stock_order_id=order.id, assembly_lot_id=assembly_lot_id,
        supplier_order_id=anchor_id,
        supplier_order_number=supplier_orders[anchor_id].order_number, status=order.status,
        status_label=("BOM 父件实际成套尾捆" if chosen_tail else "BOM 父件整套预印｜未成套库存"),
        label_policy_source="product_master_current",
        quantity_basis="actual_assembled_tail" if chosen_tail else "frozen_bom_full_bundles", template_version=template,
        template_dimensions=template_dimensions(template), label_layout=layout,
        plan_fingerprint=fingerprint, production_task_count=0, label_count=len(labels),
        review_required=False, review_messages=[], printable=True,
        plans=[label_plan], labels=labels, excluded_items=[], source_selection_keys=[key],
        planned_tail_quantity=target % units, actual_tail_candidates=tails if not chosen_tail else [],
        latest_printed_job=latest)


def validate_frozen_source(job, package, links):
    from app.services.production_label_operations import ProductionLabelOperationError as Error
    plans = package.get("plans") or []
    if (links or job.delivery_id or job.material_requisition_id or not job.supplier_order_id
            or package.get("supplier_order_id") != job.supplier_order_id
            or len(plans) != 1 or plans[0].get("stock_order_id") != package.get("stock_order_id")
            or plans[0].get("selection_key") != (f"bom-parent-tail:{package.get('stock_order_id')}:{plans[0].get('product_id')}:{package.get('assembly_lot_id')}"
                if package.get("assembly_lot_id") else f"bom-parent:{package.get('stock_order_id')}:{plans[0].get('product_id')}")
            or any(label.get("selection_key") != plans[0]["selection_key"] for label in package.get("labels") or [])):
        raise Error("BOM 父件标签来源校验失败，已停止读取或补打")


def assert_current(db, job, package, *, claim=False):
    from app.services.production_label_operations import ProductionLabelOperationError as Error, _claim_package_product_versions, _claim_customer_version
    if claim:
        _claim_package_product_versions(db, package)
        plan = package["plans"][0]
        _claim_customer_version(db, customer_id=plan["customer_id"], expected_version=plan["customer_version"])
        db.expire_all()
    try:
        current = build_package(db, package["stock_order_id"], assembly_lot_id=package.get("assembly_lot_id"))
    except ProductionPackagingLabelError as error:
        raise Error(f"BOM 父件标签来源已变化，请刷新：{error}") from error
    if current["plan_fingerprint"] != job.plan_fingerprint:
        raise Error("BOM 父件标签计划已变化，请重新加载")


def prepare_job(db, *, stock_order_id, assembly_lot_id=None, key, fingerprint, count, operator_id):
    from app.services.production_label_operations import (
        ProductionLabelOperationError as Error, PackagingLabelJobResult, _claim_package_product_versions,
        _canonical_hash, _canonical_json, _load_job_package, _assert_prepared_job_is_current,
    )
    from app.services.production_packaging_label import apply_delivery_packaging_label_print_counts
    request_hash = _canonical_hash(dict(stock_order_id=stock_order_id,
        assembly_lot_id=assembly_lot_id, fingerprint=fingerprint, count=count))
    repeated = db.scalar(select(ProductionPackagingLabelPrintJob).where(ProductionPackagingLabelPrintJob.idempotency_key == key))
    if repeated:
        if repeated.request_hash != request_hash or repeated.operator_id != operator_id:
            raise Error("幂等键已用于不同的父件标签请求")
        package = _load_job_package(repeated, db)
        _assert_prepared_job_is_current(db, repeated, package)
        return PackagingLabelJobResult(repeated, package, True)
    package = build_package(db, stock_order_id, assembly_lot_id=assembly_lot_id)
    if package["plan_fingerprint"] != fingerprint:
        raise Error("父件标签计划已变化，请刷新")
    _claim_package_product_versions(db, package)
    db.expire_all()
    current = build_package(db, stock_order_id, assembly_lot_id=assembly_lot_id)
    if current["plan_fingerprint"] != fingerprint:
        raise Error("父件标签来源已变化，请刷新")
    key_name = package["plans"][0]["selection_key"]
    frozen = apply_delivery_packaging_label_print_counts(package, {key_name: count})
    job = ProductionPackagingLabelPrintJob(supplier_order_id=package["supplier_order_id"],
        idempotency_key=key, request_hash=request_hash, operator_id=operator_id,
        template_version=package["template_version"], plan_fingerprint=fingerprint,
        payload_json=_canonical_json(frozen), payload_hash=_canonical_hash(frozen), status="prepared")
    db.add(job)
    db.flush()
    return PackagingLabelJobResult(job, frozen, False)

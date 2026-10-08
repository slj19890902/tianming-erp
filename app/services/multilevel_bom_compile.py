"""Compile real master identities into the existing immutable material rows.

No order lines, inventory or procurement facts are manufactured here. Each
product occurs once, including shared descendants. The original graph retains
all parent/child edges; a flattened material row is not a replacement recipe.
Order entry freezes this contract through the atomic procurement adapter.
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.services.composite_bom import _bom_row_values, _snapshot_kwargs, _validate_die_cut_mold
from app.services.incoming_receipts import _snapshot_component_types, _snapshot_physical_pieces
from app.services.multilevel_bom_master import load_master_structure
from app.services.multilevel_bom_plan import BomEdge, BomModes, BomPlanError, FrozenBom, MaterialRoute, ProductNode, PurchaseUnits, plan_bom
from app.services.bom_physical_quantities import resolve_bom_sheet_yield
from app.services.sheet_cutting_settings import component_settings
from app.services.multilevel_bom_execution_boundary import ExecutionWindow


@dataclass(frozen=True)
class CompiledMasterBom:
    graph: FrozenBom
    # Detached ORM rows, never added to a session by the compiler.
    snapshots: tuple[SalesOrderItemBomComponent, ...]
    execution_window: ExecutionWindow | None = None
    rule_revision_id: int | None = None
    history_source_ids: frozenset[int] = frozenset()


def physical_routes(snapshot):
    """Use the same whole/cover/base and splice rules as actual receipt."""
    try:
        routes = tuple(MaterialRoute(kind, _snapshot_physical_pieces(snapshot, kind),
                       resolve_bom_sheet_yield(snapshot, strict=True, component_type=kind).yield_per_sheet)
                       for kind in _snapshot_component_types(snapshot))
    except ValueError as error:
        raise BomPlanError(str(error)) from error
    return routes


def compile_master_order_bom(db, order_item, *, root_order_snapshot=False):
    if type(order_item.quantity) is not int or order_item.quantity <= 0:
        raise BomPlanError("订单数量必须为正整数")
    structure = load_master_structure(db, order_item.product_id)
    # An explicitly priced physical parent has its own order line. Its separately
    # priced accessories belong to their own lines, never this line's pick plan.
    if (order_item.combination_role == "priced_component"
            and order_item.combination_parent_product_id == order_item.product_id):
        root_id = order_item.product_id
        if structure["profiles"].get(root_id) not in {"manufactured", "purchased"}:
            raise BomPlanError("分项计价父件必须是明确的实体产品")
        if any(e["relation"] != "accompany" for e in structure["edges"] if e["parent_id"] == root_id):
            raise BomPlanError("组装消耗子件不能与实体父件重复分别计价，请先核对配套关系")
        structure = {**structure, "products": {root_id: structure["products"][root_id]},
            "profiles": {root_id: structure["profiles"][root_id]}, "edges": [], "order": [root_id],
            "material_mode": "expand_children", "delivery_mode": "parent"}
    products, profiles = structure["products"], structure["profiles"]
    if order_item.product_id not in profiles:
        raise BomPlanError("父产品尚未配置真实BOM库存来源")
    incoming = {pid: [] for pid in products}
    rows = {r.id: r for r in db.scalars(select(ProductBomComponent).where(
        ProductBomComponent.id.in_([e["bom_component_id"] for e in structure["edges"]])))}
    for edge in structure["edges"]:
        incoming[edge["child_id"]].append(rows[edge["bom_component_id"]])
    root = products[order_item.product_id]
    snapshots, nodes = [], []
    # Configuration conflicts on a shared product must not be silently netted.
    # In real-product mode, the child editor owns its current manufacturing
    # process. Edge-local safety limits and spare quantities remain explicit;
    # cached mold/flag/path fields must not override a later child master edit.
    process_fields = ("mold_max_yield_per_sheet", "spare_sheet_quantity")
    for position, pid in enumerate(structure["order"], 1):
        product = products[pid]
        source = profiles.get(pid, "purchased" if product.supply_mode == "external_purchase" else "manufactured")
        if source == "manufactured" and (product.supply_mode == "external_purchase" or product.is_virtual_composite_parent):
            raise BomPlanError("自制来源与产品材料结构不一致，请先维护常用箱")
        values = [_bom_row_values(row, parent_code=products[row.parent_product_id].product_code,
                                 fallback_position=index) for index, row in enumerate(incoming[pid], 1)]
        if values:
            relation = dict(values[0])
            if any(any(value[key] != relation[key] for key in process_fields) for value in values[1:]):
                raise BomPlanError("共享子件的工艺或备料配置不一致，不能合并报料")
        else:
            setting = component_settings(getattr(product, "sheet_cutting_settings", None))
            die_cut = setting.is_die_cut if setting else product.box_category == "die_cut"
            relation = dict(id=None, is_die_cut=die_cut, die_cut_path=product.die_cut_path,
                mold_tool_id=product.mold_tool_id if die_cut else None, mold_max_yield_per_sheet=None,
                spare_sheet_quantity=0, display_mode="internal_only", show_on_delivery=True,
                is_required=True, remark=None)
        relation.update(quantity_per_set=Decimal(1), display_order=position,
                        internal_component_code=f"GRAPH-{pid}")
        # A shared node has several true edges, so do not lie about one owning
        # source edge. Those relationships remain in the frozen graph.
        if len(values) > 1:
            relation["id"] = None
        if source == "manufactured":
            setting = component_settings(getattr(product, "sheet_cutting_settings", None))
            die_cut = setting.is_die_cut if setting else product.box_category == "die_cut"
            if die_cut and any(v["mold_max_yield_per_sheet"] is not None
                    and v["mold_tool_id"] not in (None, product.mold_tool_id) for v in values):
                raise BomPlanError("子件模具已变更，请先核对BOM最大模切出数")
            relation.update(is_die_cut=die_cut,
                die_cut_path=product.die_cut_path if die_cut else None,
                mold_tool_id=product.mold_tool_id if die_cut else None,
                mold_max_yield_per_sheet=relation.get("mold_max_yield_per_sheet") if die_cut else None)
        if source != "manufactured":
            # An assembled/purchased stock identity has no own board process.
            # Old descriptive master fields must not demand a phantom mold.
            relation.update(_has_own_sheet=False, is_die_cut=False, die_cut_path=None, mold_tool_id=None,
                            mold_max_yield_per_sheet=None, spare_sheet_quantity=0)
        mold = _validate_die_cut_mold(db, product, position=position,
            is_die_cut=relation["is_die_cut"], mold_tool_id=relation["mold_tool_id"])
        relation["mold_tool_id"] = mold.id if mold else None
        relation["_mold_tool"] = mold
        snapshot = SalesOrderItemBomComponent(**_snapshot_kwargs(SalesOrderItemBomComponent,
            order_item=order_item, parent=root, component=product, relation=relation))
        if root_order_snapshot and pid == root.id and source == "manufactured":
            # Public creation already validated and froze the order's chosen
            # material/process. NULL is intentional, not a reason to read the
            # master again. Child identities retain their own master inputs.
            snapshot.snapshot_component_material_id = order_item.material_id
            snapshot.snapshot_component_material = order_item.snapshot_material
            snapshot.snapshot_component_supplier_name = order_item.snapshot_supplier_name
            snapshot.snapshot_component_layer_count = order_item.layer_count
            snapshot.snapshot_component_flute_type = order_item.flute_type
            snapshot.snapshot_component_production_notes = order_item.snapshot_production_notes
            for field in ("report_length_mm", "report_width_mm", "crease_type", "crease_left_mm",
                          "crease_middle_mm", "crease_right_mm", "report_notes", "base_report_length_mm",
                          "base_report_width_mm", "base_crease_type", "base_crease_left_mm",
                          "base_crease_middle_mm", "base_crease_right_mm", "base_report_notes",
                          "splice_mode", "pieces_per_box", "flap_mm"):
                setattr(snapshot, f"snapshot_component_{field}", getattr(order_item, f"snapshot_{field}"))
            snapshot.snapshot_component_default_cutting_mode = order_item.special_process
            snapshot.sheet_cutting_settings_snapshot = order_item.sheet_cutting_settings_snapshot
        snapshot.snapshot_schema_version = 5
        routes = physical_routes(snapshot) if source == "manufactured" else ()
        purchase_units = None
        if source == "purchased":
            if product.supply_mode != "external_purchase":
                raise BomPlanError("外购节点须先在常用箱维护外购资料和采购比例")
            purchase_units = PurchaseUnits(product.external_packaging_purchase_unit,
                str(product.external_packaging_default_order_quantity_basis),
                str(product.external_packaging_default_purchase_quantity_basis)).validated()
        nodes.append(ProductNode(pid, product.customer_id, product.version,
                                 product.product_name, product.unit, source, routes, purchase_units))
        snapshots.append(snapshot)
    modes = None
    if structure["material_mode"] is not None:
        inventory = profiles[root.id] if profiles[root.id] in {"assembled", "separate"} else "body"
        modes = BomModes(structure["material_mode"], inventory, structure["delivery_mode"])
    graph = FrozenBom(root.id, root.customer_id, tuple(nodes), tuple(
        BomEdge(e["parent_id"], e["child_id"], e["quantity"], e["relation"]) for e in structure["edges"]), modes)
    gross = {d.product_id: d.required_units for d in plan_bom(graph, 1).products}
    for snapshot in snapshots:
        multiplier = gross[snapshot.component_product_id]
        total = multiplier * order_item.quantity
        if total > 9_999_999_999:
            raise BomPlanError("多级BOM展开数量超出数据库精度")
        snapshot.quantity_per_set = Decimal(multiplier)
        snapshot.required_piece_quantity = Decimal(total)
    return CompiledMasterBom(graph, tuple(snapshots))

"""Compile real master identities into the existing immutable material rows.

No order lines, inventory or procurement facts are manufactured here. Each
product occurs once, including shared descendants. The original graph retains
all parent/child edges; a flattened material row is not a replacement recipe.
Order entry stays gated until the execution adapters consume this contract.
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.services.composite_bom import _bom_row_values, _snapshot_kwargs, _validate_die_cut_mold
from app.services.incoming_receipts import _snapshot_component_types, _snapshot_physical_pieces
from app.services.multilevel_bom_master import load_master_structure
from app.services.multilevel_bom_plan import BomEdge, BomPlanError, FrozenBom, MaterialRoute, ProductNode, plan_bom
from app.services.requisition_quantities import cutting_factor, normalize_cutting_mode


@dataclass(frozen=True)
class CompiledMasterBom:
    graph: FrozenBom
    # Detached ORM rows, never added to a session by the compiler.
    snapshots: tuple[SalesOrderItemBomComponent, ...]


def physical_routes(snapshot):
    """Use the same whole/cover/base and splice rules as actual receipt."""
    mode = normalize_cutting_mode(snapshot.snapshot_component_default_cutting_mode, strict=True)
    return tuple(MaterialRoute(kind, _snapshot_physical_pieces(snapshot, kind), cutting_factor(mode))
                 for kind in _snapshot_component_types(snapshot))


def compile_master_order_bom(db, order_item):
    if type(order_item.quantity) is not int or order_item.quantity <= 0:
        raise BomPlanError("订单数量必须为正整数")
    structure = load_master_structure(db, order_item.product_id)
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
    process_fields = ("is_die_cut", "die_cut_path", "mold_tool_id", "mold_max_yield_per_sheet",
                      "spare_sheet_quantity")
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
            die_cut = product.box_category == "die_cut"
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
        if source != "manufactured":
            # An assembled/purchased stock identity has no own board process.
            # Old descriptive master fields must not demand a phantom mold.
            relation.update(is_die_cut=False, die_cut_path=None, mold_tool_id=None,
                            mold_max_yield_per_sheet=None, spare_sheet_quantity=0)
        mold = _validate_die_cut_mold(db, product, position=position,
            is_die_cut=relation["is_die_cut"], mold_tool_id=relation["mold_tool_id"])
        relation["mold_tool_id"] = mold.id if mold else None
        relation["_mold_tool"] = mold
        snapshot = SalesOrderItemBomComponent(**_snapshot_kwargs(SalesOrderItemBomComponent,
            order_item=order_item, parent=root, component=product, relation=relation))
        snapshot.snapshot_schema_version = 5
        routes = physical_routes(snapshot) if source == "manufactured" else ()
        nodes.append(ProductNode(pid, product.customer_id, product.version,
                                 product.product_name, product.unit, source, routes))
        snapshots.append(snapshot)
    graph = FrozenBom(root.id, root.customer_id, tuple(nodes), tuple(
        BomEdge(e["parent_id"], e["child_id"], e["quantity"], e["relation"]) for e in structure["edges"]))
    gross = {d.product_id: d.required_units for d in plan_bom(graph, 1).products}
    for snapshot in snapshots:
        multiplier = gross[snapshot.component_product_id]
        total = multiplier * order_item.quantity
        if total > 9_999_999_999:
            raise BomPlanError("多级BOM展开数量超出数据库精度")
        snapshot.quantity_per_set = Decimal(multiplier)
        snapshot.required_piece_quantity = Decimal(total)
    return CompiledMasterBom(graph, tuple(snapshots))

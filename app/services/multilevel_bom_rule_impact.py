"""Compare two immutable rules for a stated remaining order quantity.

This is the common quantity/identity input to a controlled rule handoff, not
permission to switch an order. Procurement and stock execution facts still
need a locked, versioned handoff; this module does not invent stock credits.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from types import SimpleNamespace

from app.services.finished_stock_identity import compiled_product_bases
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from app.services.multilevel_bom_purchase_units import purchase_quantity_for_stock
from app.services.multilevel_bom_snapshot import dump_graph, graph_hash


def rule_quantity_impact(previous, proposed, *, remaining_quantity):
    """Never subtract unlike units or equate a name/code with an identity.

    Plans below are gross requirements before stock allocation. In particular,
    sheet deltas are not purchase cancellations: existing procurement, receipt,
    usable stock and losses must be reconciled by the transaction adapter.
    """
    if (previous.graph.root_id != proposed.graph.root_id
            or previous.graph.customer_id != proposed.graph.customer_id):
        raise BomPlanError("规则切换必须保持订单真实父产品及客户身份")
    old_ids = {row.sales_order_item_id for row in previous.snapshots}
    new_ids = {row.sales_order_item_id for row in proposed.snapshots}
    if (len(old_ids) != 1 or old_ids != new_ids
            or any(type(value) is not int or value <= 0 for value in old_ids)):
        raise BomPlanError("新旧规则来源不属于同一订单")
    old_bases, new_bases = compiled_product_bases(previous), compiled_product_bases(proposed)
    old_plan = plan_bom(previous.graph, remaining_quantity)
    new_plan = plan_bom(proposed.graph, remaining_quantity)
    if any(remaining_quantity > row.order_set_quantity
           for compiled in (previous, proposed) for row in compiled.snapshots):
        raise BomPlanError("待切换数量超过新旧规则冻结执行数量")

    def side(compiled, plan):
        demands = {row.product_id: row for row in plan.products}
        picking = dict(plan.picking)
        result = {}
        for node in compiled.graph.nodes:
            demand = demands[node.product_id]
            result[node.product_id] = dict(
                name=node.name, version=node.version, source=node.source, unit=node.unit,
                required_quantity=demand.required_units,
                pick_quantity=picking.get(node.product_id, 0),
                materials=[asdict(row) for row in plan.materials if row.product_id == node.product_id],
                purchase=(dict(quantity=str(purchase_quantity_for_stock(node, demand.make_units)),
                    unit=node.purchase_units.purchase_unit)
                    if node.source == "purchased" else None))
        return result

    old, new = side(previous, old_plan), side(proposed, new_plan)
    rows = []
    for pid in sorted(old.keys() | new.keys()):
        before, after = old.get(pid), new.get(pid)
        same_unit = before is not None and after is not None and before["unit"] == after["unit"]
        # A virtual node never authorizes stock carry-over, even if its master
        # happens to have the same dimensions as a former physical product.
        compatible = bool(same_unit and before["source"] != "separate"
            and after["source"] != "separate" and old_bases[pid] == new_bases[pid])
        rows.append(dict(product_id=pid, before=before, after=after,
            physical_identity_compatible=compatible,
            required_delta=(after["required_quantity"] - before["required_quantity"] if same_unit else None),
            pick_delta=(after["pick_quantity"] - before["pick_quantity"] if same_unit else None),
            change="added" if before is None else "removed" if after is None else "retained"))
    return dict(order_item_id=next(iter(old_ids)), remaining_quantity=remaining_quantity,
        previous_graph_hash=graph_hash(dump_graph(previous.graph)),
        proposed_graph_hash=graph_hash(dump_graph(proposed.graph)),
        previous_modes=asdict(previous.graph.modes) if previous.graph.modes else None,
        proposed_modes=asdict(proposed.graph.modes) if proposed.graph.modes else None,
        products=rows, inventory_credits_applied=False,
        material_scope="剩余订单的毛需求及材料张数，未抵扣库存，不是已采购或已实收数量",
        identity_scope="相容仅表示实物规则匹配；仍须核对具体批次、货位、工艺依据、预占、成本和版本")


@dataclass(frozen=True)
class RuleRequirementReview:
    previous: object
    proposed: object
    impact: dict
    document: str
    checksum: str


def review_current_rule_requirements(db, *, order_item_id, customer_id):
    """Read current execution rules and compile a detached remaining recipe.

    This deliberately does not issue an executable cutover token. Its hash
    binds the quantity projection only; the handoff adapter must also capture
    procurement, reservations, lots, locations, costs and historical effects.
    """
    from app.models.order import Order, OrderItem
    from app.services.multilevel_bom_compile import compile_master_order_bom
    from app.services.multilevel_bom_cutover_review import _row
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_production_revision import production_basis

    if db.new or db.dirty or db.deleted:
        raise BomPlanError("请先保存或撤销未提交修改，再核对规则")
    db.expire_all()
    with db.no_autoflush:
        item = db.get(OrderItem, order_item_id)
        order = db.get(Order, item.order_id) if item else None
        if item is None or order is None or order.customer_id != customer_id:
            raise BomPlanError("核对订单不存在或客户不一致")
        if (item.is_force_closed or item.quantity <= 0
                or not 0 <= (item.delivered_quantity or 0) < item.quantity
                or order.status in {"cancelled", "closed", "dead", "completed", "archived", "delivered"}):
            raise BomPlanError("只可核对未完成订单的剩余规则")
        previous = read_compiled_order_bom(db, item.id)
        if previous is None:
            raise BomPlanError("订单缺少真实冻结图，请使用旧BOM转换核对")
        remaining = item.quantity - (item.delivered_quantity or 0)
        detached = SimpleNamespace(**{
            column.key: getattr(item, column.key) for column in OrderItem.__table__.columns})
        detached.quantity = remaining
        proposed = compile_master_order_bom(db, detached)
        # Retain all original source IDs. A future writer appends these new
        # sources at fresh positions; the template edge is not their identity.
        from sqlalchemy import select, func
        from app.models.product_bom import SalesOrderItemBomComponent
        offset = db.scalar(select(func.max(SalesOrderItemBomComponent.display_order)).where(
            SalesOrderItemBomComponent.sales_order_item_id == item.id)) or 0
        for position, row in enumerate(proposed.snapshots, offset + 1):
            row.display_order = position
            row.product_bom_component_id = None
        impact = rule_quantity_impact(previous, proposed, remaining_quantity=remaining)
        document = json.dumps(dict(schema=1, scope="quantity_projection_only",
            order=_row(order), item=_row(item), previous_basis=production_basis(previous),
            proposed_basis=production_basis(proposed), impact=impact),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return RuleRequirementReview(previous, proposed, impact, document,
            hashlib.sha256(document.encode("utf-8")).hexdigest())

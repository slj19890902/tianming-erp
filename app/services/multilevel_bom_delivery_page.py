"""Validate and project all graph orders on a delivery page in one batch."""
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select

from app.models.multilevel_bom import OrderBomGraphProduct
from app.services.multilevel_bom_execution_boundary import cutover_roles_by_order
from app.services.multilevel_bom_rule_history import rule_histories_by_order, project_order_rule_history
from app.services.multilevel_bom_plan import BomPlanError
from app.services.composite_bom_workflow import project_graph_delivery_demands
from app.services.multilevel_bom_production_versions import production_revisions_by_order_ids


def project_page_graph_demands(db, *, graphs, cutovers, order_items, orders, snapshots, demands):
    if set(cutovers) - set(graphs):
        raise BomPlanError("订单多级BOM冻结关系缺失，不能按旧组件显示")
    grouped = defaultdict(list)
    for snapshot in snapshots:
        grouped[snapshot.sales_order_item_id].append(snapshot)
        if (snapshot.snapshot_schema_version or 0) >= 5 and snapshot.sales_order_item_id not in graphs:
            raise BomPlanError("订单多级BOM冻结关系缺失，不能按旧组件显示")
    identities = defaultdict(set)
    if graphs:
        for row in db.scalars(select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id.in_(graphs))):
            identities[row.order_item_id].add((row.product_id, row.product_version))
    revisions = production_revisions_by_order_ids(db, graphs)
    histories = rule_histories_by_order(db, graphs)
    roles = cutover_roles_by_order(db, cutovers)
    roots = {}
    for item_id, row in graphs.items():
        item = order_items[item_id]
        compiled = project_order_rule_history(header=row, item=item, order=orders.get(item.order_id),
            identities=identities[item_id], cutover=cutovers.get(item_id),
            rows_with_roles=((source, roles[item_id].get(source.id)) for source in grouped[item_id]),
            production_rows=revisions[item_id], history=histories[item_id])
        demands[item_id] = project_graph_delivery_demands(compiled, item, demands.get(item_id, []))
        roots[item_id] = next(d.snapshot_id for d in demands[item_id] if d.is_graph_root)
    return roots


@dataclass
class GraphSummaryContracts:
    picks: dict = field(default_factory=dict)
    roots: dict = field(default_factory=dict)
    excluded: set = field(default_factory=set)
    # Old sources still count on actual historical delivery documents, but
    # must never be reintroduced into a pending dispatch's current demand.
    history_ids: set = field(default_factory=set)
    delivered_before: dict = field(default_factory=dict)


def summary_graph_contracts(db, item_ids):
    """Fixed query families; no current master or per-order query fallback."""
    if not item_ids:
        return GraphSummaryContracts()
    from app.models.order import Order, OrderItem
    from app.models.multilevel_bom import OrderBomGraph, OrderBomExecutionCutover
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.services.multilevel_bom_plan import plan_bom
    rows = db.execute(select(OrderItem, Order, OrderBomGraph, OrderBomExecutionCutover).join(Order, Order.id == OrderItem.order_id)
        .join(OrderBomGraph, OrderBomGraph.order_item_id == OrderItem.id)
        .outerjoin(OrderBomExecutionCutover, OrderBomExecutionCutover.order_item_id == OrderItem.id)
        .where(OrderItem.id.in_(item_ids))).all()
    if len(rows) != len(set(item_ids)):
        raise BomPlanError("订单多级BOM冻结关系缺失")
    snapshots = defaultdict(list)
    for row in db.scalars(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))):
        snapshots[row.sales_order_item_id].append(row)
    identities = defaultdict(set)
    for row in db.scalars(select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id.in_(item_ids))):
        identities[row.order_item_id].add((row.product_id, row.product_version))
    revisions = production_revisions_by_order_ids(db, item_ids)
    histories = rule_histories_by_order(db, item_ids)
    roles = cutover_roles_by_order(db, {item.id for item, _, _, cutover in rows if cutover is not None})
    result = GraphSummaryContracts()
    for item, order, row, cutover in rows:
        compiled = project_order_rule_history(header=row, item=item, order=order,
            identities=identities[item.id], cutover=cutover,
            rows_with_roles=((source, roles[item.id].get(source.id)) for source in snapshots[item.id]),
            production_rows=revisions[item.id], history=histories[item.id])
        graph = compiled.graph
        window = compiled.execution_window
        quantity = window.execution_quantity if window else item.quantity
        result.delivered_before[item.id] = window.delivered_before if window else 0
        result.history_ids.update(source_id for source_id, role in roles[item.id].items() if role == "history")
        result.history_ids.update(compiled.history_source_ids)
        required = dict(plan_bom(compiled.graph, 1).picking)
        for snapshot in compiled.snapshots:
            if snapshot.component_product_id not in required:
                result.excluded.add(snapshot.id)
                continue
            multiplier = required[snapshot.component_product_id]
            result.picks[snapshot.id] = (multiplier, quantity * multiplier)
            if snapshot.component_product_id == graph.root_id:
                result.roots[item.id] = snapshot.id
    return result

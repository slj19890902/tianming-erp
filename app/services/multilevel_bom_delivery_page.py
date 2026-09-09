"""Validate and project all graph orders on a delivery page in one batch."""
from collections import defaultdict

from sqlalchemy import select

from app.models.multilevel_bom import OrderBomGraphProduct
from app.services.multilevel_bom_orders import validate_order_graph_rows, validate_compiled_order_rows
from app.services.multilevel_bom_plan import BomPlanError
from app.services.composite_bom_workflow import project_graph_delivery_demands
from app.services.multilevel_bom_production_versions import production_revisions_by_order_ids, project_production_versions


def project_page_graph_demands(db, *, graphs, order_items, orders, snapshots, demands):
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
    roots = {}
    for item_id, row in graphs.items():
        item = order_items[item_id]
        graph = validate_order_graph_rows(row, item, orders.get(item.order_id), identities[item_id])
        compiled = validate_compiled_order_rows(graph, tuple(grouped[item_id]))
        compiled = project_production_versions(compiled, revisions[item_id])
        demands[item_id] = project_graph_delivery_demands(compiled, item, demands.get(item_id, []))
        roots[item_id] = next(d.snapshot_id for d in demands[item_id] if d.is_graph_root)
    return roots


def summary_graph_contracts(db, item_ids):
    """Fixed query families; no current master or per-order query fallback."""
    if not item_ids:
        return {}, {}, set()
    from app.models.order import Order, OrderItem
    from app.models.multilevel_bom import OrderBomGraph
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.services.multilevel_bom_plan import plan_bom
    rows = db.execute(select(OrderItem, Order, OrderBomGraph).join(Order, Order.id == OrderItem.order_id)
        .join(OrderBomGraph, OrderBomGraph.order_item_id == OrderItem.id).where(OrderItem.id.in_(item_ids))).all()
    if len(rows) != len(set(item_ids)):
        raise BomPlanError("订单多级BOM冻结关系缺失")
    snapshots = defaultdict(list)
    for row in db.scalars(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))):
        snapshots[row.sales_order_item_id].append(row)
    identities = defaultdict(set)
    for row in db.scalars(select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id.in_(item_ids))):
        identities[row.order_item_id].add((row.product_id, row.product_version))
    revisions = production_revisions_by_order_ids(db, item_ids)
    picks, roots, excluded = {}, {}, set()
    for item, order, row in rows:
        graph = validate_order_graph_rows(row, item, order, identities[item.id])
        compiled = validate_compiled_order_rows(graph, tuple(snapshots[item.id]))
        compiled = project_production_versions(compiled, revisions[item.id])
        required = dict(plan_bom(compiled.graph, 1).picking)
        for snapshot in compiled.snapshots:
            if snapshot.component_product_id not in required:
                excluded.add(snapshot.id)
                continue
            multiplier = required[snapshot.component_product_id]
            picks[snapshot.id] = (multiplier, item.quantity * multiplier)
            if snapshot.component_product_id == graph.root_id:
                roots[item.id] = snapshot.id
    return picks, roots, excluded

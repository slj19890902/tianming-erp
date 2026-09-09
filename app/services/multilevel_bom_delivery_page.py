"""Validate and project all graph orders on a delivery page in one batch."""
from collections import defaultdict

from sqlalchemy import select

from app.models.multilevel_bom import OrderBomGraphProduct
from app.services.multilevel_bom_orders import validate_order_graph_rows, validate_compiled_order_rows
from app.services.multilevel_bom_plan import BomPlanError
from app.services.composite_bom_workflow import project_graph_delivery_demands


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
    roots = {}
    for item_id, row in graphs.items():
        item = order_items[item_id]
        graph = validate_order_graph_rows(row, item, orders.get(item.order_id), identities[item_id])
        compiled = validate_compiled_order_rows(graph, tuple(grouped[item_id]))
        demands[item_id] = project_graph_delivery_demands(compiled, item, demands.get(item_id, []))
        roots[item_id] = next(d.snapshot_id for d in demands[item_id] if d.is_graph_root)
    return roots

"""Resolve receipt quantities through frozen product identities, never names."""
from sqlalchemy import select

from app.models.multilevel_bom import OrderBomGraph
from app.models.order_external_packaging import SalesOrderItemExternalComponentCandidate
from app.services.multilevel_bom_external_identity import read_external_node
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_purchase_units import cumulative_receipt_conversion


def graph_receipt_conversions(db, normalized, totals, *, customer_id):
    """Validate every graph line before the caller inserts any receipt fact.

    Returns purchase-item -> (whole node units, cumulative loose purchase units).
    Ordinary external receipts and replenishment retain their existing paths.
    This is quantity preparation, not inventory posting or assembly completion.
    """
    ids = {row.sales_order_item_id for row, _ in normalized if row.sales_order_item_id is not None}
    if not ids:
        return {}
    graph_ids = set(db.scalars(select(OrderBomGraph.order_item_id).where(OrderBomGraph.order_item_id.in_(ids))))
    compiled = {oid: read_compiled_order_bom(db, oid) for oid in graph_ids}
    result = {}
    for row, quantity in normalized:
        graph = compiled.get(row.sales_order_item_id)
        if graph is None:
            continue
        link = read_external_node(db, row.order_component_id)
        if link is None or link.order_item_id != row.sales_order_item_id:
            raise BomPlanError('真实BOM外购收料缺少有效节点关联')
        candidate = db.get(SalesOrderItemExternalComponentCandidate, row.order_candidate_id)
        node = next(n for n in graph.graph.nodes if n.product_id == link.product_id)
        if (graph.graph.customer_id != customer_id or candidate is None
                or candidate.order_component_id != row.order_component_id
                or candidate.customer_scope_id_snapshot not in (None, customer_id)
                or candidate.external_product_id_snapshot != row.external_product_id_snapshot
                or candidate.external_product_version_snapshot != row.external_product_version_snapshot
                or candidate.purchase_unit_snapshot != row.purchase_unit
                or node.purchase_units.purchase_unit != row.purchase_unit):
            raise BomPlanError('真实BOM收料客户、采购候选或单位不一致')
        result[row.id] = cumulative_receipt_conversion(node,
            received_before=totals.get(row.id, 0), received_now=quantity)
    return result

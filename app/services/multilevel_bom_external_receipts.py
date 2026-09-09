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


def post_graph_receipt_inventory(db, *, purchase_item, receipt_item, customer_id, operator_id):
    """Post the real child, never a fabricated parent production completion.

    Caller owns the receipt transaction, permissions and order lock. Costs use
    the frozen external purchase contract, not the current paper master.
    """
    import json
    from decimal import Decimal, ROUND_HALF_UP
    from app.services.multilevel_bom_external_costs import receipt_output_cost
    from app.core.time_contract import beijing_today, utc_now_naive
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in

    quantity = int(receipt_item.converted_finished_quantity)
    if quantity <= 0:
        return None
    link = read_external_node(db, purchase_item.order_component_id)
    if link is None or link.order_item_id != purchase_item.sales_order_item_id:
        raise BomPlanError('外购入库缺少真实产品身份')
    compiled = read_compiled_order_bom(db, link.order_item_id)
    if compiled.graph.customer_id != customer_id:
        raise BomPlanError('外购入库客户不一致')
    node = next(n for n in compiled.graph.nodes if n.product_id == link.product_id)
    target = _receipt_auto_finished_ground_target(db, claim=True,
        customer_id=customer_id, product_id=node.product_id)
    lot = manual_finished_in(db, customer_id=customer_id, product_id=node.product_id,
        location_id=target.location.id, quantity=quantity, stock_date=beijing_today(),
        source_type='purchase_reserve', source_ref_type='bom_external_receipt',
        source_ref_id=receipt_item.id, remarks='外购子件收料入库', operator_id=operator_id,
        idempotency_key=f'bom-external-receipt:{receipt_item.id}',
        expected_layout_version=target.layout_version, require_empty_pallet=False,
        movement_reason='外购子件收料入库')
    # Ledger uses boxes for whole finished units; the real product owns 套/只.
    snapshot = next(s for s in compiled.snapshots if s.id == link.bom_snapshot_id)
    lot.finished_detail.inventory_code_snapshot = snapshot.snapshot_component_product_code
    lot.finished_detail.product_name_snapshot = node.name
    detail = receipt_output_cost(db, receipt_item.id)
    lot.estimated_unit_cost_snapshot = (Decimal(detail['capitalized_material_cost']) / quantity).quantize(
        Decimal('0.0001'), rounding=ROUND_HALF_UP)
    lot.estimated_square_price_snapshot = None
    lot.estimated_cost_area_m2_snapshot = None
    lot.cost_snapshot_source = 'external_bom_receipt'
    lot.cost_snapshot_at = utc_now_naive()
    lot.cost_snapshot_detail_json = json.dumps(detail, ensure_ascii=False)
    db.flush()
    return lot

"""Physical external procurement closure, separate from finished stock coverage."""
from collections import defaultdict
from decimal import Decimal
from sqlalchemy import select, func

from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseItem, ExternalPackagingPurchaseOrder,
    ExternalPackagingPurchaseCancellation, ExternalPackagingReceiptItem,
)
from app.services.multilevel_bom_external_identity import read_external_node
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_purchase_units import cumulative_receipt_conversion
from app.services.external_receipt_state import active_receipt_item


def external_graph_receipts_closed(db, *, item, requirements):
    nodes = {n.product_id:n for n in requirements.compiled.graph.nodes if n.source == 'purchased'}
    if not nodes:
        return True
    cancelled = select(ExternalPackagingPurchaseCancellation.purchase_order_id)
    purchases = list(db.scalars(select(ExternalPackagingPurchaseItem).join(ExternalPackagingPurchaseOrder,
        ExternalPackagingPurchaseOrder.id == ExternalPackagingPurchaseItem.purchase_order_id).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == item.id,
            ExternalPackagingPurchaseOrder.status == 'confirmed',
            ExternalPackagingPurchaseOrder.id.not_in(cancelled))))
    from app.services.external_packaging_receiving import _received_totals
    totals = _received_totals(db, {p.id for p in purchases})
    outputs = dict(db.execute(select(ExternalPackagingReceiptItem.purchase_item_id,
        func.sum(ExternalPackagingReceiptItem.converted_finished_quantity)).where(
            active_receipt_item(),
            ExternalPackagingReceiptItem.purchase_item_id.in_([p.id for p in purchases]))
        .group_by(ExternalPackagingReceiptItem.purchase_item_id)).all())
    received_stock = defaultdict(int)
    pending = False
    for purchase in purchases:
        link = read_external_node(db, purchase.order_component_id)
        if (link is None or link.order_item_id != item.id or link.product_id not in nodes
                or purchase.sales_order_id != item.order_id
                or purchase.purchase_unit != nodes[link.product_id].purchase_units.purchase_unit):
            raise BomPlanError('外购收齐判断缺少有效真实BOM来源')
        received = totals.get(purchase.id, Decimal(0))
        if received > purchase.purchase_quantity:
            raise BomPlanError('外购实收数量超出冻结采购量')
        pending |= received < purchase.purchase_quantity
        produced, _ = cumulative_receipt_conversion(nodes[link.product_id], received_before=0, received_now=received)
        if produced != int(outputs.get(purchase.id, 0)):
            raise BomPlanError('外购实收产出与冻结换算不一致')
        received_stock[link.product_id] += produced
    return not pending and all(received_stock[p.product_id] >= p.make_units
        for p in requirements.plan.products if p.product_id in nodes)

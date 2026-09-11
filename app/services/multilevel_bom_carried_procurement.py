"""Credit handed-off supplier obligations once, in real stock units."""
from collections import defaultdict
from decimal import Decimal
from sqlalchemy import select

from app.models.multilevel_bom import OrderBomExternalComponent
from app.models.order import OrderItem
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseItem, ExternalPackagingPurchaseOrder,
    ExternalPackagingPurchaseCancellation, ExternalPackagingReceiptItem,
)
from app.services.external_receipt_state import active_receipt_item
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_purchase_units import cumulative_receipt_conversion
from app.services.multilevel_bom_source_handoffs import current_source_handoffs


def carried_purchase_stock(db, compiled):
    """Old output is covered by explicit stock transfer, not supplier credit.

    Count only new-version receipt output plus what the remaining old contract
    can still produce, including its loose-unit carry. Round new purchasing
    after subtracting these whole stock units, never subtract unlike units.
    """
    from app.services.multilevel_bom_external_identity import external_receipt_contract, external_receipt_execution_contract
    handoffs = [row for row in current_source_handoffs(db, compiled) if row.source_kind == "purchased"]
    if not handoffs:
        return {}
    order_id = compiled.snapshots[0].sales_order_item_id
    item = db.get(OrderItem, order_id)
    source_ids = {row.source_snapshot_id for row in handoffs}
    component_ids = select(OrderBomExternalComponent.external_component_id).where(
        OrderBomExternalComponent.order_item_id == order_id,
        OrderBomExternalComponent.bom_snapshot_id.in_(source_ids))
    purchases = list(db.scalars(select(ExternalPackagingPurchaseItem).join(ExternalPackagingPurchaseOrder,
        ExternalPackagingPurchaseOrder.id == ExternalPackagingPurchaseItem.purchase_order_id).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == order_id,
            ExternalPackagingPurchaseItem.order_component_id.in_(component_ids),
            ExternalPackagingPurchaseOrder.status == "confirmed")))
    cancelled = set(db.scalars(select(ExternalPackagingPurchaseCancellation.purchase_order_id).where(
        ExternalPackagingPurchaseCancellation.purchase_order_id.in_([row.purchase_order_id for row in purchases]))))
    receipts = defaultdict(list)
    for row in db.scalars(select(ExternalPackagingReceiptItem).where(active_receipt_item(),
            ExternalPackagingReceiptItem.purchase_item_id.in_([row.id for row in purchases]))):
        receipts[row.purchase_item_id].append(row)
    current_ids = {row.id for row in compiled.snapshots}
    credits = defaultdict(int)
    for purchase in purchases:
        link, original, _, _ = external_receipt_contract(db, purchase.order_component_id, compiled=compiled)
        node = next(node for node in original.graph.nodes if node.product_id == link.product_id)
        if item is None or purchase.sales_order_id != item.order_id or purchase.purchase_unit != node.purchase_units.purchase_unit:
            raise BomPlanError("交接采购的订单或采购单位不一致")
        received = sum((row.received_quantity for row in receipts[purchase.id]), Decimal(0))
        if received > purchase.purchase_quantity:
            raise BomPlanError("交接采购实收超过原合同数量")
        produced, _ = cumulative_receipt_conversion(node, received_before=0, received_now=received)
        if produced != sum(row.converted_finished_quantity for row in receipts[purchase.id]):
            raise BomPlanError("交接采购累计实收产出不符合冻结换算")
        pending = Decimal(0) if purchase.purchase_order_id in cancelled else purchase.purchase_quantity - received
        future, _ = cumulative_receipt_conversion(node, received_before=received, received_now=pending)
        credits[link.product_id] += future
        for receipt in receipts[purchase.id]:
            _, execution = external_receipt_execution_contract(db, receipt.id)
            if any(row.id in current_ids and row.component_product_id == link.product_id for row in execution.snapshots):
                credits[link.product_id] += receipt.converted_finished_quantity
    return dict(credits)

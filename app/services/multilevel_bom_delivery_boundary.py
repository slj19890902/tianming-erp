"""Preflight cancellation against an explicit BOM execution cutover.

Runs under the delivery cancellation transaction/write claim. This is not a
permission bypass or an operational writer for legacy cutovers.
"""
from sqlalchemy import select

from app.models.multilevel_bom import OrderBomExecutionCutover, OrderBomCutoverSource
from app.models.product_bom import BomComponentDirectDeliveryAllocation
from app.models.warehouse_inventory import DeliveryInventoryAllocation, InventoryReservation
from app.services.multilevel_bom_execution_boundary import execution_window
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError


def validate_cancel_execution_boundary(db, *, item, delivery_item_ids, quantity):
    cutover = db.get(OrderBomExecutionCutover, item.id)
    if cutover is None:
        return
    if type(quantity) is not int or quantity <= 0:
        raise BomPlanError("撤销送货数量无效")
    # Verify the source manifest too, not only the numeric boundary.
    if read_compiled_order_bom(db, item.id) is None:
        raise BomPlanError("BOM转换缺少冻结来源，不能撤销送货")
    execution_window(order_quantity=item.quantity,
        delivered_quantity=int(item.delivered_quantity or 0) - quantity, cutover=cutover)
    history = select(OrderBomCutoverSource.snapshot_id).where(
        OrderBomCutoverSource.order_item_id == item.id, OrderBomCutoverSource.role == "history")
    direct = db.scalar(select(BomComponentDirectDeliveryAllocation.id).where(
        BomComponentDirectDeliveryAllocation.delivery_item_id.in_(delivery_item_ids),
        BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id.in_(history),
        BomComponentDirectDeliveryAllocation.consumed_quantity > BomComponentDirectDeliveryAllocation.reversed_quantity
    ).limit(1))
    stocked = db.scalar(select(DeliveryInventoryAllocation.id).join(InventoryReservation,
        InventoryReservation.id == DeliveryInventoryAllocation.reservation_id).where(
        DeliveryInventoryAllocation.delivery_item_id.in_(delivery_item_ids),
        InventoryReservation.sales_order_item_bom_component_id.in_(history),
        DeliveryInventoryAllocation.credited_requirement_quantity > DeliveryInventoryAllocation.reversed_requirement_quantity
    ).limit(1))
    if direct is not None or stocked is not None:
        raise BomPlanError("送货单使用BOM转换前的历史来源，须先核对历史转换，不能直接撤销")

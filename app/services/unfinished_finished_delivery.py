"""Finished stock already reserved to an order can ship before its task finishes.

Only current, physically backed main-item reservations count here. Production
plans, semi-finished credits and historical consumed credits are never output.
The existing dispatch transaction retains identity, version and stock checks.
"""
from fractions import Fraction

from sqlalchemy import select

from app.models import InventoryLot, InventoryReservation, OrderItem


def unfinished_finished_quantities(db, item_ids):
    ids=sorted({int(value) for value in item_ids})
    if not ids:
        return {}
    stock_remaining=(InventoryReservation.reserved_stock_quantity
        - InventoryReservation.consumed_stock_quantity
        - InventoryReservation.released_stock_quantity)
    credit_remaining=(InventoryReservation.credited_requirement_quantity
        - InventoryReservation.consumed_requirement_quantity
        - InventoryReservation.released_requirement_quantity)
    rows=db.execute(select(
        InventoryReservation.order_item_id,credit_remaining,
        InventoryReservation.requirement_quantity_denominator,
        OrderItem.quantity,OrderItem.delivered_quantity,
    ).join(InventoryLot,InventoryLot.id==InventoryReservation.inventory_lot_id)
     .join(OrderItem,OrderItem.id==InventoryReservation.order_item_id)
     .where(InventoryReservation.order_item_id.in_(ids),
        InventoryReservation.order_id==OrderItem.order_id,
        InventoryReservation.reservation_type=='finished_order',
        InventoryReservation.sales_order_item_bom_component_id.is_(None),
        InventoryReservation.status.in_(('active','partial')),
        InventoryLot.inventory_type=='finished',InventoryLot.status=='active',
        stock_remaining>0,stock_remaining<=InventoryLot.quantity_reserved,
        credit_remaining>0))
    credits={};remaining={}
    for item_id,credit,denominator,ordered,delivered in rows:
        credits[item_id]=credits.get(item_id,Fraction())+Fraction(int(credit),int(denominator))
        remaining[item_id]=max(int(ordered)-int(delivered or 0),0)
    return {item_id:min(int(credit),remaining[item_id]) for item_id,credit in credits.items()}


def dispatched_finished_coverage_allows_primary(db, task, item):
    """Prove earlier dispatch consumed the task's existing finished coverage.

    Called under the production order lock. A delivered counter alone cannot
    authorize production: every delivered customer unit must have an active,
    unreversed allocation against this same main item's finished reservation.
    Component, semi-finished and unallocated historical deliveries fail closed.
    """
    from app.models import Delivery, DeliveryItem, DeliveryInventoryAllocation

    delivered = int(item.delivered_quantity or 0)
    if (task.sales_order_item_bom_component_id is not None
            or delivered <= 0
            or delivered > int(task.finished_coverage_snapshot or 0)):
        return False
    allocation = DeliveryInventoryAllocation
    credit = (allocation.credited_requirement_quantity
              - allocation.reversed_requirement_quantity)
    stock = allocation.consumed_stock_quantity - allocation.reversed_stock_quantity
    rows = db.execute(
        select(credit, allocation.requirement_quantity_denominator)
        .join(DeliveryItem, DeliveryItem.id == allocation.delivery_item_id)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(InventoryReservation, InventoryReservation.id == allocation.reservation_id)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            DeliveryItem.order_item_id == item.id,
            DeliveryItem.is_current.is_(True),
            Delivery.status == 'dispatched',
            InventoryReservation.order_id == item.order_id,
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.reservation_type == 'finished_order',
            InventoryLot.inventory_type == 'finished',
            allocation.status.in_(('active', 'partial')),
            credit > 0, stock > 0,
        )
    )
    return sum((Fraction(int(value), int(denominator))
                for value, denominator in rows), Fraction()) == delivered

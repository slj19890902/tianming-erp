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

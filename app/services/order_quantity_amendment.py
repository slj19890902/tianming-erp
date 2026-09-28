"""Change customer demand without reversing production or delivery facts."""
from fractions import Fraction

from sqlalchemy import select, update

from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.delivery_quantities import (
    reservation_customer_quantity, reservation_physical_quantity,
    reservation_requirement_numerator,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError, _balances, _movement, _finished_reservation_status,
    utc_now_naive,
)


def release_excess_finished_demand(db, *, item, remaining, actor_id, key):
    """Only outstanding parent finished stock; all consumed/source facts stay frozen.

    Call under the order write lock after validating delivery commitments. Keep
    FIFO reservations for the remaining customer demand and release the excess.
    Component/raw/semi reservations belong to production and are not rewritten.
    """
    rows = db.scalars(select(InventoryReservation).where(
        InventoryReservation.order_item_id == item.id,
        InventoryReservation.reservation_type == 'finished_order',
        InventoryReservation.sales_order_item_bom_component_id.is_(None),
        InventoryReservation.status.in_(('active', 'partial')),
    ).order_by(InventoryReservation.id)).all()
    keep = Fraction(remaining)
    released = []
    for row in rows:
        outstanding = (row.reserved_stock_quantity - row.consumed_stock_quantity
                       - row.released_stock_quantity)
        if outstanding <= 0:
            continue
        credit = reservation_customer_quantity(row, outstanding)
        retained = min(keep, credit)
        # Fractional customer credits may span lots. Never round stock pieces.
        retained_stock = reservation_physical_quantity(row, retained)
        keep -= retained
        quantity = outstanding - retained_stock
        if not quantity:
            continue
        lot = db.get(InventoryLot, row.inventory_lot_id)
        if lot is None or lot.quantity_reserved < quantity:
            raise WarehouseInventoryError('库存预占余额已变化，请刷新后重试', 409)
        before = _balances(lot)
        now = utc_now_naive()
        result = db.execute(update(InventoryLot).where(
            InventoryLot.id == lot.id, InventoryLot.version == lot.version,
            InventoryLot.quantity_reserved >= quantity,
        ).values(quantity_available=InventoryLot.quantity_available + quantity,
                 quantity_reserved=InventoryLot.quantity_reserved - quantity,
                 version=InventoryLot.version + 1, last_movement_at=now))
        if result.rowcount != 1:
            raise WarehouseInventoryError('库存版本已变化，请刷新后重试', 409)
        row.released_stock_quantity += quantity
        row.released_requirement_quantity += reservation_requirement_numerator(row, quantity)
        row.released_by, row.released_at = actor_id, now
        row.release_reason = '客户调整订单数量，超出剩余需求的成品转为可用库存'
        row.status = _finished_reservation_status(row)
        db.flush()
        db.refresh(lot)
        _movement(db, lot=lot, movement_type='release_reserve', quantity=quantity,
                  before=before, operator_id=actor_id, reason=row.release_reason,
                  idempotency_key=f'quantity:{key}:{row.id}', reservation_id=row.id,
                  related_order_id=item.order_id, related_order_item_id=item.id)
        released.append(dict(reservation_id=row.id, stock_quantity=quantity))
    return released

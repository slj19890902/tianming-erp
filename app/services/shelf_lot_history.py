"""Read-only delivery facts for a lot already authorized by the caller."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_contract import utc_naive_to_api
from app.models.delivery import Delivery
from app.models.warehouse_inventory import InventoryMovement


def shelf_delivery_history(db: Session, lot_id: int, visible_customer_ids: set[int] | None) -> list[dict]:
    # Actual consumption and formal dispatch must both exist. Never match by SKU text.
    query = (select(Delivery).join(InventoryMovement, InventoryMovement.related_delivery_id == Delivery.id)
        .where(InventoryMovement.inventory_lot_id == lot_id,
               InventoryMovement.movement_type == 'consume',
               Delivery.status == 'dispatched', Delivery.dispatched_at.is_not(None))
        .distinct().order_by(Delivery.dispatched_at.desc(), Delivery.id.desc()))
    if visible_customer_ids is not None:
        query = query.where(Delivery.customer_id.in_(visible_customer_ids))
    return [{"delivery_id": row.id, "delivery_number": row.delivery_number,
             "dispatched_at": utc_naive_to_api(row.dispatched_at)} for row in db.scalars(query)]

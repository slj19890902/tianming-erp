"""Read-only delivery facts for a lot already authorized by the caller."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_contract import utc_naive_to_api
from app.models.delivery import Delivery
from app.models.warehouse_inventory import InventoryMovement, InventoryLot, FinishedGoodsInventoryDetail
from app.models.production import ProductionCompletion
from app.models.order import Order, OrderItem
from app.services.location_candidates import load_warehouse_location_projection_contexts
from app.services.warehouse_location_address import employee_location_name


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


def shelf_related_inventory(db: Session, lot: InventoryLot, visible_customer_ids: set[int] | None) -> dict:
    detail = lot.finished_detail
    result = {"source_order": None, "same_product_locations": []}
    if detail is None or detail.owner_customer_id is None or detail.product_id is None:
        return result
    if visible_customer_ids is not None and detail.owner_customer_id not in visible_customer_ids:
        return result
    if lot.source_ref_type == 'production_completion' and lot.source_ref_id:
        source = db.execute(select(Order, OrderItem).join(OrderItem, OrderItem.order_id == Order.id)
            .join(ProductionCompletion, ProductionCompletion.order_item_id == OrderItem.id)
            .where(ProductionCompletion.id == lot.source_ref_id, Order.customer_id == detail.owner_customer_id)).first()
        if source:
            order, item = source
            result['source_order'] = {"order_id": order.id, "order_item_id": item.id, "order_number": order.order_number}
    # This is a location lookup, not a reservation recommendation. Show physical
    # quantities and status; the existing order service alone decides eligibility.
    query = select(InventoryLot).join(FinishedGoodsInventoryDetail,
        FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).where(
        FinishedGoodsInventoryDetail.product_id == detail.product_id,
        FinishedGoodsInventoryDetail.owner_customer_id == detail.owner_customer_id,
        InventoryLot.inventory_type == 'finished', InventoryLot.unit == lot.unit, InventoryLot.status.in_(['active', 'frozen']),
        InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0)
    for field in ('length_mm', 'width_mm', 'height_mm', 'material_code_snapshot', 'flute_type_snapshot'):
        query = query.where(getattr(FinishedGoodsInventoryDetail, field) == getattr(detail, field))
    rows = list(db.scalars(query.order_by(InventoryLot.warehouse_location_id, InventoryLot.id)))
    contexts = load_warehouse_location_projection_contexts(db, [row.location for row in rows])
    for row in rows:
        context = contexts.get(row.warehouse_location_id, {})
        result['same_product_locations'].append({
            "lot_id": row.id, "location_id": row.warehouse_location_id,
            "location_name": employee_location_name(row.location, area=context.get('area'),
                floor=context.get('floor'), area_sequence=context.get('area_sequence')),
            "floor": row.location.warehouse_floor, "lot_number": row.lot_number,
            "physical_quantity": row.quantity_available + row.quantity_reserved + row.quantity_damaged,
            "available_quantity": row.quantity_available, "reserved_quantity": row.quantity_reserved,
            "unit": row.unit, "status": row.status,
        })
    return result

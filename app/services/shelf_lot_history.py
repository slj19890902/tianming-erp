"""Read-only delivery facts for a lot already authorized by the caller."""
from sqlalchemy import select, or_
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
    result['bom_relations'] = shelf_bom_relations(db, detail.product_id, detail.owner_customer_id)
    if lot.source_ref_type == 'production_completion' and lot.source_ref_id:
        source = db.execute(select(Order, OrderItem).join(OrderItem, OrderItem.order_id == Order.id)
            .join(ProductionCompletion, ProductionCompletion.order_item_id == OrderItem.id)
            .where(ProductionCompletion.id == lot.source_ref_id, Order.customer_id == detail.owner_customer_id)).first()
        if source:
            order, item = source
            result['source_order'] = {"order_id": order.id, "order_item_id": item.id, "order_number": order.order_number, "customer_po": order.customer_po}
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


def shelf_bom_relations(db: Session, product_id: int, customer_id: int) -> list[dict]:
    """Current recipe navigation, explicitly NOT an allocation or frozen order.

    Use relational IDs and owner scope in both directions. Each position keeps
    its real reservation/order facts; same SKU text never establishes a link.
    """
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.multilevel_bom import ProductBomInventoryRelation
    from app.models.warehouse_inventory import InventoryReservation
    edges = list(db.execute(select(ProductBomComponent, ProductBomInventoryRelation.relation)
        .join(ProductBomInventoryRelation, ProductBomInventoryRelation.bom_component_id == ProductBomComponent.id)
        .where(or_(ProductBomComponent.parent_product_id == product_id,
                   ProductBomComponent.component_product_id == product_id))
        .order_by(ProductBomComponent.id)))
    related = []
    for edge, relation in edges:
        parent = edge.component_product_id == product_id
        pid = edge.parent_product_id if parent else edge.component_product_id
        product = db.get(Product, pid)
        if product is None or product.customer_id != customer_id:
            continue
        related.append(dict(product_id=pid, product_code=product.product_code,
            product_name=product.product_name, direction='parent' if parent else 'child',
            relation=relation, quantity_per_set=int(edge.quantity_per_set),
            basis='current_master_not_allocation', locations=[]))
    if not related:
        return []
    lots = list(db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail,
        FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).where(
        FinishedGoodsInventoryDetail.product_id.in_({r['product_id'] for r in related}),
        FinishedGoodsInventoryDetail.owner_customer_id == customer_id,
        InventoryLot.inventory_type == 'finished', InventoryLot.status.in_(['active', 'frozen']),
        InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0)
        .order_by(InventoryLot.warehouse_location_id, InventoryLot.id)))
    contexts = load_warehouse_location_projection_contexts(db, [l.location for l in lots if l.location])
    reservations = {}
    if lots:
        for res, order in db.execute(select(InventoryReservation, Order)
            .join(Order, Order.id == InventoryReservation.order_id).where(
                InventoryReservation.inventory_lot_id.in_([l.id for l in lots]), Order.customer_id == customer_id,
                InventoryReservation.reserved_stock_quantity > InventoryReservation.consumed_stock_quantity + InventoryReservation.released_stock_quantity)):
            reservations.setdefault(res.inventory_lot_id, []).append(dict(
                order_id=order.id, order_item_id=res.order_item_id, order_number=order.order_number, customer_po=order.customer_po,
                reservation_id=res.id, quantity=res.reserved_stock_quantity-res.consumed_stock_quantity-res.released_stock_quantity))
    for row in related:
        for lot in lots:
            if lot.finished_detail.product_id != row['product_id']:
                continue
            ctx = contexts.get(lot.warehouse_location_id, {})
            row['locations'].append(dict(lot_id=lot.id, location_id=lot.warehouse_location_id,
                location_name=employee_location_name(lot.location, area=ctx.get('area'),
                    floor=ctx.get('floor'), area_sequence=ctx.get('area_sequence')) if lot.location else '待归位',
                quantity=lot.quantity_available+lot.quantity_reserved+lot.quantity_damaged,
                unit=lot.unit, status=lot.status, reservations=reservations.get(lot.id, [])))
    return related

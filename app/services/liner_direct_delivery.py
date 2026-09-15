"""Read-only eligibility for already finished, one-to-one liner sheets.

No completion or destination is invented: the existing semi reservation is
consumed (and reversed) by the normal delivery transaction at its source lot.
"""
from sqlalchemy import select
from app.models.order import OrderItem
from app.models.product import Product
from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement
from app.services.box_type_rules import box_type_code


def liner_direct_coverage(db, item: OrderItem) -> int:
    from app.services.semi_finished_inventory import (
        requirement_signature, is_direct_semi_finished_match, finished_order_source_coverage,
    )
    product = db.get(Product, item.product_id)
    if (product is None or box_type_code(product.box_style) != "liner"
            or product.box_category == "die_cut" or item.composite_fulfillment_mode_snapshot):
        return 0
    tasks = list(db.scalars(select(ProductionTask).where(ProductionTask.order_item_id == item.id)))
    if any(str(t.print_content_snapshot or '').strip() not in {'', '无印刷', '无', '否', '不印刷'} for t in tasks):
        return 0
    if db.scalar(select(ProductionCompletion.id).where(
            ProductionCompletion.order_item_id == item.id, ProductionCompletion.status == 'posted').limit(1)):
        return 0
    requirements = list(db.scalars(select(OrderItemSemiRequirement).where(OrderItemSemiRequirement.order_item_id == item.id)))
    if len(requirements) != 1:
        return 0
    requirement = requirements[0]
    if (requirement.component_type != 'whole' or requirement.pieces_per_box != 1
            or requirement.stock_yield_per_sheet != 1):
        return 0
    rows = db.execute(select(InventoryReservation, InventoryLot).join(
        InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id).where(
        InventoryReservation.semi_requirement_id == requirement.id,
        InventoryReservation.reservation_type == 'semi_order',
        InventoryReservation.status != 'cancelled')).all()
    covered = 0
    for reservation, lot in rows:
        credit = int(reservation.credited_requirement_quantity or 0) - int(reservation.released_requirement_quantity or 0)
        if credit <= 0:
            continue
        if (reservation.sales_order_item_bom_component_id is not None or reservation.yield_factor != 1
                or lot.semi_finished_detail is None or lot.semi_finished_detail.sheet_type != 'net_sheet' or not is_direct_semi_finished_match(
                    lot.semi_finished_detail, expected=requirement_signature(requirement),
                    layer_count=item.layer_count, crease_type=item.snapshot_crease_type,
                    crease_left_mm=item.snapshot_crease_left_mm,
                    crease_middle_mm=item.snapshot_crease_middle_mm,
                    crease_right_mm=item.snapshot_crease_right_mm)):
            return 0
        covered += credit
    # Mixed processing requirements must not be silently declared completed.
    finished = db.scalars(select(InventoryReservation).where(
        InventoryReservation.order_item_id == item.id,
        InventoryReservation.reservation_type == 'finished_order',
        InventoryReservation.sales_order_item_bom_component_id.is_(None),
        InventoryReservation.status != 'cancelled')).all()
    if covered + finished_order_source_coverage(finished) < int(item.quantity or 0):
        return 0
    return min(covered, int(item.quantity or 0))


def direct_liner_item_ids(db):
    items = db.scalars(select(OrderItem).join(
        InventoryReservation, InventoryReservation.order_item_id == OrderItem.id).where(
        InventoryReservation.reservation_type == 'semi_order',
        InventoryReservation.status != 'cancelled',
        OrderItem.delivered_quantity < OrderItem.quantity,
        OrderItem.is_force_closed.is_(False)).distinct()).all()
    return [item.id for item in items if liner_direct_coverage(db, item) > 0]

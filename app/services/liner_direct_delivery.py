"""Read-only eligibility for already finished, one-to-one liner sheets.

No completion or destination is invented: the existing semi reservation is
consumed (and reversed) by the normal delivery transaction at its source lot.
"""
from dataclasses import replace
from sqlalchemy import select
from app.models.order import OrderItem
from app.models.product import Product
from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement
from app.services.box_type_rules import box_type_code


def liner_direct_coverage(db, item: OrderItem, *, product: Product | None = None) -> int:
    from app.services.semi_finished_inventory import (
        finished_order_source_coverage, ensure_semi_finished_lot_eligibility, requirement_signature,
    )
    # List and dashboard status projections can pre-load the small product
    # set for many items.  Reuse that authoritative object when available;
    # retaining the lookup fallback keeps direct callers unchanged.
    product = product if product is not None else db.get(Product, item.product_id)
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
        detail = lot.semi_finished_detail
        # Eligibility is based on the accepted reservation and physical work,
        # not on today's master material. Keep the actual lot/cost unchanged.
        if (reservation.sales_order_item_bom_component_id is not None or reservation.yield_factor != 1
                or detail is None or detail.sheet_type != 'net_sheet'
                or detail.pieces_per_box != 1 or detail.stock_yield_per_sheet != 1
                or sorted((detail.board_length_mm, detail.board_width_mm)) != sorted((requirement.board_length_mm, requirement.board_width_mm))
                or reservation.order_item_id != item.id or reservation.order_id != item.order_id
                or detail.owner_customer_id not in (None, requirement.customer_id)
                or ((detail.owner_customer_id is None or detail.normalized_material_code != requirement.normalized_material_code)
                    and reservation.warning_acknowledged_by is None)):
            return 0
        from app.services.warehouse_inventory import WarehouseInventoryError
        try:
            expected = requirement_signature(requirement)
            # This existing one-to-one reservation already records acceptance of
            # the actual material. Recheck its scope, face and physical eligibility
            # against that material without changing the product, lot or cost.
            if (detail.normalized_material_code != expected.normalized_material_code
                    and reservation.warning_acknowledged_by is not None):
                expected = replace(expected, normalized_material_code=detail.normalized_material_code)
            ensure_semi_finished_lot_eligibility(db, lot=lot, product_id=item.product_id,
                customer_id=requirement.customer_id, expected=expected)
        except WarehouseInventoryError:
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

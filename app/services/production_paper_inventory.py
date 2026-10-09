"""Read exact pre-existing reservations for employee picking instructions."""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.warehouse_inventory import (
    InventoryLot, InventoryPalletItem, InventoryReservation, OrderItemSemiRequirement,
    FinishedGoodsInventoryDetail,
)
from app.services.location_candidates import load_warehouse_location_projection_contexts
from app.services.production_inventory_locations import lot_location
from app.services.warehouse_display_units import lot_display_unit
from app.models.production import ProductionCompletion


def paper_inventory_sources(db, order_item_ids, *, cutoff):
    if not order_item_ids:
        return {}
    query = (select(InventoryReservation, InventoryLot, OrderItemSemiRequirement.component_type)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .outerjoin(OrderItemSemiRequirement,
                   OrderItemSemiRequirement.id == InventoryReservation.semi_requirement_id)
        .where(InventoryReservation.order_item_id.in_(order_item_ids),
               InventoryReservation.status.in_(['active', 'partial', 'consumed']),
               ~select(ProductionCompletion.id).where(
                   ProductionCompletion.id == InventoryLot.source_ref_id,
                   InventoryLot.source_ref_type == 'production_completion',
                   ProductionCompletion.order_item_id == InventoryReservation.order_item_id,
               ).exists())
        .options(selectinload(InventoryLot.location),
                 selectinload(InventoryLot.finished_detail).selectinload(FinishedGoodsInventoryDetail.product),
                 selectinload(InventoryLot.pallet_item).selectinload(InventoryPalletItem.pallet))
        .order_by(InventoryReservation.id))
    # Newly produced finished goods are not pre-production stock deductions.
    if cutoff is not None:
        query = query.where(InventoryReservation.created_at <= cutoff)
    records = db.execute(query).all()
    locations = {lot.warehouse_location_id: lot.location for _, lot, _ in records if lot.location}
    contexts = load_warehouse_location_projection_contexts(db, locations.values())
    result = defaultdict(list)
    for reservation, lot, component_type in records:
        remaining = max(0, int(reservation.reserved_stock_quantity)
            - int(reservation.consumed_stock_quantity) - int(reservation.released_stock_quantity))
        consumed = int(reservation.consumed_stock_quantity)
        if not remaining and not consumed:
            continue
        address = lot_location(lot, locations, contexts) if remaining else {}
        result[(reservation.order_item_id, reservation.sales_order_item_bom_component_id)].append({
            'reservation_id': reservation.id,
            'inventory_lot_id': lot.id,
            'lot_number': lot.lot_number,
            'kind': 'finished' if reservation.reservation_type.startswith('finished_') else 'semi_finished',
            'component_type': component_type,
            'requisition_item_id': reservation.requisition_item_id,
            'quantity': remaining,
            'consumed_quantity': consumed,
            'unit': {'boxes': '只', 'sheets': '张', 'pieces': '片'}.get(lot_display_unit(lot), lot_display_unit(lot)),
            'location_name': address.get('current_warehouse_location_name') or ('位置待确认' if remaining else None),
            'location_id': address.get('current_warehouse_location_id'),
            'location_issue': address.get('current_location_issue'),
        })
    return dict(result)


def component_picks(sources, *, order_item_id, bom_id=None, component_type='whole'):
    # Shared finished coverage is the parent order's fact. Do not apply it to BOM pieces.
    return [dict(row) for row in sources.get((order_item_id, bom_id), [])
            if row['kind'] == 'finished' or row['component_type'] in (None, component_type)]

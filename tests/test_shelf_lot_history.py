from datetime import date, datetime
from sqlalchemy import select, func
from app.models.delivery import Delivery
from app.models.warehouse_inventory import InventoryMovement
from app.services.shelf_lot_history import shelf_delivery_history, shelf_related_inventory
from app.services.warehouse_twin_dashboard import _lot_payload
from test_warehouse_inventory_foundation import db, finished_lot, seed_other_customer_product


def test_delivery_history_requires_dispatch_consumption_and_customer_scope(db):
    lot = finished_lot(db)
    other, _ = seed_other_customer_product(db, 'other')
    customer_id = lot.finished_detail.owner_customer_id
    ids = []
    for index, (status, timestamp, movement_type, owner) in enumerate([
        ('dispatched', datetime(2026, 9, 8, 1), 'consume', customer_id),
        ('pending', None, 'consume', customer_id),
        ('voided', datetime(2026, 9, 8, 3), 'consume', customer_id),
        ('dispatched', None, 'consume', customer_id),
        ('dispatched', datetime(2026, 9, 8, 4), 'location_transfer', customer_id),
        ('dispatched', datetime(2026, 9, 8, 5), 'consume', other.id),
    ]):
        delivery = Delivery(delivery_number=f'SHELF-{index}', customer_id=owner,
            delivery_date=date(2026, 9, 8), status=status, dispatched_at=timestamp)
        db.add(delivery)
        db.flush()
        ids.append(delivery.id)
        changes = {f'{prefix}_{field}': 0 for prefix in ('before', 'after')
                   for field in ('available', 'reserved', 'consumed', 'damaged', 'scrapped')}
        db.add(InventoryMovement(movement_number=f'HISTORY-{index}', inventory_lot_id=lot.id,
            movement_type=movement_type, related_delivery_id=delivery.id, quantity=1, unit=lot.unit, **changes))
    db.commit()
    before = db.scalar(select(func.count()).select_from(InventoryMovement))
    assert [row['delivery_id'] for row in shelf_delivery_history(db, lot.id, {customer_id})] == [ids[0]]
    assert [row['delivery_id'] for row in shelf_delivery_history(db, lot.id, None)] == [ids[5], ids[0]]
    assert shelf_delivery_history(db, lot.id, set()) == []
    assert shelf_delivery_history(db, lot.id + 1000, None) == []
    assert db.scalar(select(func.count()).select_from(InventoryMovement)) == before
    assert lot.quantity_available == 20


def test_dashboard_date_does_not_invent_unknown_historical_date(db):
    lot = finished_lot(db)
    assert _lot_payload(lot, date.today())['stock_date'] == lot.stock_date.isoformat()
    lot.stock_date_accuracy = 'unknown'
    assert _lot_payload(lot, date.today())['stock_date'] is None


def test_same_product_lookup_preserves_customer_snapshot_and_quantity_boundaries(db):
    from app.services.warehouse_inventory import manual_finished_in
    lot = finished_lot(db)
    detail = lot.finished_detail
    second = manual_finished_in(db, customer_id=detail.owner_customer_id, product_id=detail.product_id,
        location_id=lot.warehouse_location_id, quantity=7, stock_date=date.today(), source_type='manual',
        remarks=None, operator_id=None, idempotency_key='same-product-second')
    db.commit()
    result = shelf_related_inventory(db, lot, {detail.owner_customer_id})
    assert result['source_order'] is None
    assert {row['lot_id'] for row in result['same_product_locations']} == {lot.id, second.id}
    assert sum(row['physical_quantity'] for row in result['same_product_locations']) == 27
    assert shelf_related_inventory(db, lot, set())['same_product_locations'] == []
    original = second.finished_detail.length_mm
    second.finished_detail.length_mm = original + 1
    db.flush()
    assert [row['lot_id'] for row in shelf_related_inventory(db, lot, None)['same_product_locations']] == [lot.id]
    second.finished_detail.length_mm = original
    second.status = 'frozen'
    db.flush()
    rows = shelf_related_inventory(db, lot, None)['same_product_locations']
    assert next(row for row in rows if row['lot_id'] == second.id)['status'] == 'frozen'
    assert lot.quantity_available == 20 and second.quantity_available == 7

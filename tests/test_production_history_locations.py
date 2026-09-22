from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.api.production import router
from app.models.production import ProductionCompletion
from app.models.order import OrderItem
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, WarehouseArea
from app.services.warehouse_inventory import manual_finished_in
from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import (
    _p181_published_map_identity, _seed_material_and_staging, _create_frozen_sources,
    _freeze_receipt_fact, _receive,
)


def test_registered_pending_stock_has_a_readable_location_without_a_physical_pallet():
    from app.services.production_inventory_locations import current_inventory
    location = WarehouseLocation(id=100, location_code='RECOUNT-PENDING', source_version='RECOUNT_PENDING',
        location_name='一楼成品待送区（待归位）', is_active=True, is_temporary=True,
        placement_status='unplaced', warehouse_type='shared', address_kind='legacy')
    lot = InventoryLot(id=20, version=1, warehouse_location_id=100, status='active',
        quantity_available=5, quantity_reserved=4, quantity_damaged=0)
    row = current_inventory([lot], {100: location}, {})
    assert row['current_inventory_quantity'] == 9
    assert row['current_inventory_status'] == 'located'
    assert row['current_warehouse_location_id'] == 100
    assert '待归位' in row['current_warehouse_location_name']
    assert row['current_warehouse_location_map_issue']  # Logical staging is not a placed map slot.


def seed_history(app, factory):
    app.include_router(router, prefix='/api/production')
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client, 'admin')
        source = _create_frozen_sources(client, factory, order_quantity=1000,
            purchase_total=1000, order_purpose=1000, stock_purpose=0)[0]
        frozen = _freeze_receipt_fact(client, source, idempotency_key='history-price')
        assert frozen.status_code == 200, frozen.text
        received = _receive(client, source, frozen.json(), quantity=1000, idempotency_key='history-receive')
        assert received.status_code == 200, received.text
    with factory() as db:
        completion = db.scalar(select(ProductionCompletion))
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        item = db.get(OrderItem, completion.order_item_id)
        loc = db.get(WarehouseLocation, lot.warehouse_location_id)
        area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == loc.area_code))
        db.add(ReceiptStagingArea(area_id=area.id))
        extra = manual_finished_in(db, customer_id=item.order.customer_id, product_id=item.product_id,
            location_id=loc.id, quantity=50, stock_date=date.today(), source_type='purchase_reserve',
            remarks='other source fixture', operator_id=1, idempotency_key='history-unrelated',
            expected_layout_version=loc.floor3_layout.version, require_empty_pallet=False)
        target = db.scalar(select(WarehouseLocation).where(WarehouseLocation.area_code == loc.area_code,
            WarehouseLocation.id != loc.id).order_by(WarehouseLocation.id))
        db.commit()
        return [lot.id, extra.id], lot.version, target.id, target.floor3_layout.version


@pytest.mark.parametrize('moved_quantity', [1000, 400])
def test_history_tracks_placed_lot_family_and_remaining_locations(requisition_app, moved_quantity):
    app, factory = requisition_app
    ids, version, target, layout_version = seed_history(app, factory)
    with factory() as db:
        original = db.get(InventoryLot, ids[0])
        completion_id = original.source_ref_id
        initial_location = db.get(ProductionCompletion, completion_id).warehouse_location_id

    def history(client):
        response = client.get('/api/production/completions', params={'page_size': 50, 'include_stock': True})
        assert response.status_code == 200, response.text
        return next(r for r in response.json()['items'] if r['id'] == completion_id)

    with TestClient(app) as client:
        _login(client, 'admin')
        response = client.post(f'/api/production/placement-stock/{ids[0]}/transfer', json=dict(
            expected_version=version, quantity=moved_quantity, location_id=target,
            expected_layout_version=layout_version, idempotency_key='history-place'))
        assert response.status_code == 200, response.text
        moved_id = response.json()['target_lot_id']
        assert moved_id != ids[0]  # Normal full-pallet placement also creates a descendant.
        row = history(client)
        assert row['current_inventory_quantity'] == 1000  # Other batches of this product are excluded.
        assert row['completion_warehouse_location_id'] == initial_location
        assert row['inventory_lot_id'] == ids[0]  # Action identity is never replaced with a display lot.
        places = {p['inventory_lot_id']: p for p in row['current_inventory_locations']}
        assert places[moved_id]['current_warehouse_location_id'] == target
        assert places[moved_id]['current_inventory_quantity'] == moved_quantity
        assert places[moved_id]['current_warehouse_location_name']
        assert places[moved_id]['current_inventory_status'] == 'located'
        assert row['current_inventory_status'] == 'located'
        assert len(places) == (1 if moved_quantity == 1000 else 2)
        if moved_quantity < 1000:
            assert places[ids[0]]['current_inventory_quantity'] == 600
            assert row['current_warehouse_location_id'] is None  # No arbitrary single map destination.

        # A second move can return to the original location while leaving other stock there.
        with factory() as db:
            moved = db.get(InventoryLot, moved_id)
            source_location = db.get(WarehouseLocation, initial_location)
            next_payload = dict(expected_version=moved.version, quantity=moved_quantity,
                location_id=initial_location, expected_layout_version=source_location.floor3_layout.version,
                idempotency_key='history-place-again')
        second = client.post(f'/api/production/placement-stock/{moved_id}/transfer', json=next_payload)
        assert second.status_code == 200, second.text
        final_id = second.json()['target_lot_id']
        with factory() as db:
            db.get(InventoryLot, final_id).status = 'frozen'
            completion = db.get(ProductionCompletion, completion_id)
            item = db.get(OrderItem, completion.order_item_id)
            item.delivered_quantity = item.quantity
            db.commit()
        row = history(client)
        assert row['is_fully_delivered'] and row['current_inventory_quantity'] == 1000
        assert row['current_warehouse_location_id'] == initial_location
        assert final_id in [p['inventory_lot_id'] for p in row['current_inventory_locations']]
        assert moved_id not in [p['inventory_lot_id'] for p in row['current_inventory_locations']]

        # A genuinely exhausted family stays empty; unrelated stock cannot fill it.
        with factory() as db:
            for lot in db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type == 'production_completion',
                    InventoryLot.source_ref_id == completion_id)):
                lot.quantity_available = lot.quantity_reserved = lot.quantity_damaged = 0
            db.commit()
        row = history(client)
        assert row['current_inventory_quantity'] == 0 and row['current_inventory_locations'] == []
        assert row['current_inventory_status'] == 'drained'
        from app.services.production_workflow import list_production_completions
        with factory() as db:
            assert not list_production_completions(db, allowed_customer_ids=[])

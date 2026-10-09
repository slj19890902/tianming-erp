"""Real HTTP contracts for the legacy whole-pallet move CAS boundary."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, event

from test_floor3_locations_api import _login, floor3_app, _ensure_location_layout_version
from test_legacy_pallet_merge_cas import merge_api, reserve, snapshot as inventory_snapshot
from app.api import warehouse as warehouse_api
from app.models.warehouse_inventory import (
    InventoryPallet, InventoryPalletItem, InventoryLot, InventoryLocationMovement,
    WarehouseLocation, WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
)
from app.services import floor3_locations, warehouse_ground_slots, warehouse_inventory


def snapshot(factory):
    result = inventory_snapshot(factory)
    with factory() as db:
        result['costs_and_frozen_details'] = [
            (lot.id, lot.estimated_unit_cost_snapshot, lot.estimated_square_price_snapshot,
             lot.estimated_cost_area_m2_snapshot, lot.cost_snapshot_source,
             lot.cost_snapshot_detail_json, lot.cost_snapshot_at,
             lot.finished_detail.product_name_snapshot, lot.finished_detail.material_code_snapshot,
             lot.finished_detail.length_mm, lot.finished_detail.width_mm, lot.finished_detail.height_mm)
            for lot in db.scalars(select(InventoryLot).order_by(InventoryLot.id))
        ]
    return result


def install_location_guards(factory, ids):
    """Install the actual existing location triggers in this synthetic DB."""
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    with factory() as db:
        for location_id in ids['locations'][:3]:
            db.get(WarehouseLocation, location_id).placement_status = 'placed'
        db.commit()
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/nn22v8x9z11_location_layout_kind.py'
    spec = importlib.util.spec_from_file_location('move_fixture_location_guards', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with factory.kw['bind'].begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module._replace_sqlite_inventory_location_guards(require_positive_balance=True)


def move_body(ids, key='legacy-move-request'):
    return dict(expected_version=1, to_location_id=ids['locations'][2],
                confirmed=True, idempotency_key=key)


def pause_before_target_claim(monkeypatch, interleave):
    original = floor3_locations._claim_empty_active_location
    called = False
    def hook(*args, **kwargs):
        nonlocal called
        if not called:
            called = True
            interleave()
        return original(*args, **kwargs)
    monkeypatch.setattr(floor3_locations, '_claim_empty_active_location', hook)


@pytest.mark.parametrize('release', [False, True], ids=['reserve', 'reserve_release'])
def test_committed_reservation_drift_rejected_without_move_writes(merge_api, monkeypatch, release):
    app, ids, factory = merge_api
    install_location_guards(factory, ids)
    with TestClient(app) as mover, TestClient(app) as writer:
        _login(mover, 'floor3-admin')
        _login(writer, 'floor3-admin')
        committed = {}
        def interleave():
            rid = reserve(writer, factory, ids, 0, 'move-concurrent-reserve')
            if release:
                response = writer.post(f'/api/warehouse/reservations/{rid}/release', json={
                    'release_reason': '移位并发夹具', 'idempotency_key': 'move-concurrent-release'})
                assert response.status_code == 200, response.text
            committed.update(snapshot(factory))
        pause_before_target_claim(monkeypatch, interleave)
        response = mover.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json=move_body(ids))
        assert response.status_code == 409, response.text
        assert snapshot(factory) == committed


@pytest.mark.parametrize('reserved', [False, True])
def test_fresh_move_preserves_balances_and_exact_replay(merge_api, reserved):
    app, ids, factory = merge_api
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        if reserved:
            reserve(client, factory, ids, 0, 'move-existing-reserve')
        before = snapshot(factory)
        body = move_body(ids)
        url = f"/api/warehouse/pallets/{ids['pallets'][0]}/move"
        result = client.post(url, json=body)
        assert result.status_code == 200, result.text
        after = snapshot(factory)
        assert after['lots'][0][1] == before['lots'][0][1] + 1
        assert after['lots'][0][2] == body['to_location_id']
        assert after['lots'][0][3:8] == before['lots'][0][3:8]
        assert after['lots'][1] == before['lots'][1]
        assert after['movements'] == before['movements']
        assert after['costs_and_frozen_details'] == before['costs_and_frozen_details']
        assert len(after['location_movements']) == len(before['location_movements']) + 1
        replay = client.post(url, json=body)
        assert replay.status_code == 200 and replay.json()['idempotent_replay'] is True
        conflict = client.post(url, json={**body, 'expected_version': 2})
        assert conflict.status_code == 409
        assert snapshot(factory) == after


@pytest.mark.parametrize('unlinked_only', [False, True])
def test_legacy_members_and_non_operational_source_can_move(merge_api, unlinked_only):
    app, ids, factory = merge_api
    with factory() as db:
        if unlinked_only:
            db.scalar(select(InventoryPalletItem).where(
                InventoryPalletItem.pallet_id == ids['pallets'][0])).inventory_lot_id = None
        else:
            db.add(InventoryPalletItem(pallet_id=ids['pallets'][0], customer_id=ids['tianhua'],
                product_id=ids['products'][1], inventory_code='21301012', product_name='旧快照',
                item_type='finished', quantity=4, unit='boxes', match_status='matched'))
        db.get(WarehouseLocation, ids['locations'][0]).is_active = False
        db.commit()
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json=move_body(ids))
        assert response.status_code == 200, response.text
        assert response.json()['pallet']['item_count'] == (1 if unlinked_only else 2)


@pytest.mark.parametrize('change', ['member', 'quantity', 'source_location', 'target_inactive',
                                  'target_layout', 'occupied', 'lot_quantity', 'lot_status',
                                  'source_status', 'link', 'added', 'source_rack'])
def test_committed_member_and_location_drift_rejected(merge_api, monkeypatch, change):
    app, ids, factory = merge_api
    body = move_body(ids)
    if change == 'target_layout':
        body['expected_target_layout_version'] = _ensure_location_layout_version(factory, ids['locations'][2])
    committed = {}
    def interleave():
        with factory() as db:
            item = db.scalar(select(InventoryPalletItem).where(InventoryPalletItem.pallet_id == ids['pallets'][0]))
            if change == 'member':
                item.product_id = ids['products'][1]
            elif change == 'quantity':
                item.quantity += 1
            elif change == 'source_location':
                db.get(InventoryPallet, ids['pallets'][0]).location_id = ids['f34_location']
            elif change == 'target_inactive':
                db.get(WarehouseLocation, ids['locations'][2]).is_active = False
            elif change == 'target_layout':
                db.get(WarehouseLocation, ids['locations'][2]).floor3_layout.version += 1
            elif change == 'occupied':
                db.get(InventoryPallet, ids['pallets'][1]).location_id = ids['locations'][2]
            elif change == 'lot_quantity':
                db.get(InventoryLot, ids['lots'][0]).quantity_available -= 1
            elif change == 'lot_status':
                db.get(InventoryLot, ids['lots'][0]).status = 'frozen'
            elif change == 'source_status':
                db.get(InventoryPallet, ids['pallets'][0]).status = 'closed'
            elif change == 'link':
                item.inventory_lot_id = None
            elif change == 'added':
                db.add(InventoryPalletItem(pallet_id=ids['pallets'][0], customer_id=ids['tianhua'],
                    product_id=ids['products'][1], inventory_code='21301012', product_name='并发旧快照',
                    item_type='finished', quantity=4, unit='boxes', match_status='matched'))
            else:
                db.get(WarehouseLocation, ids['locations'][0]).storage_type = 'rack'
            db.commit()
        committed.update(snapshot(factory))
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        pause_before_target_claim(monkeypatch, interleave)
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json=body)
        assert response.status_code == 409, response.text
        assert snapshot(factory) == committed


def test_same_key_completed_during_target_wait_returns_exact_replay(merge_api, monkeypatch):
    app, ids, factory = merge_api
    body = move_body(ids)
    url = f"/api/warehouse/pallets/{ids['pallets'][0]}/move"
    committed = {}
    with TestClient(app) as first, TestClient(app) as second:
        _login(first, 'floor3-admin')
        _login(second, 'floor3-admin')
        def interleave():
            response = second.post(url, json=body)
            assert response.status_code == 200, response.text
            committed.update(snapshot(factory))
        pause_before_target_claim(monkeypatch, interleave)
        response = first.post(url, json=body)
        assert response.status_code == 200 and response.json()['idempotent_replay'] is True
        assert snapshot(factory) == committed


def seed_ground_occupancy(factory, ids):
    with factory() as db:
        occupancy = WarehouseGroundOccupancy(pallet_id=ids['pallets'][0],
            primary_location_id=ids['locations'][0], footprint_kind='single',
            capacity_quantity=100, created_by=ids['admin'])
        db.add(occupancy)
        db.flush()
        db.add(WarehouseGroundOccupancySlot(occupancy_id=occupancy.id,
            location_id=ids['locations'][0], slot_sequence=1))
        db.commit()


def ground_snapshot(factory):
    with factory() as db:
        return ([(o.id, o.status, o.version, o.released_by, o.released_at)
                 for o in db.scalars(select(WarehouseGroundOccupancy))],
                [(s.id, s.status, s.released_at) for s in db.scalars(select(WarehouseGroundOccupancySlot))])


@pytest.mark.parametrize('fault', ['ground', 'movement', 'audit'])
def test_move_failure_rolls_back_inventory_occupancy_slots_and_audit(merge_api, monkeypatch, fault):
    app, ids, factory = merge_api
    seed_ground_occupancy(factory, ids)
    before, ground_before = snapshot(factory), ground_snapshot(factory)
    if fault == 'ground':
        original = warehouse_ground_slots.release_ground_occupancy_for_pallet
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('legacy-move-injected')
        monkeypatch.setattr(warehouse_ground_slots, 'release_ground_occupancy_for_pallet', fail)
    elif fault == 'movement':
        def fail(mapper, connection, movement):
            if movement.movement_type == 'move':
                raise RuntimeError('legacy-move-injected')
        event.listen(InventoryLocationMovement, 'before_insert', fail)
    else:
        def fail(*args, **kwargs):
            raise RuntimeError('legacy-move-injected')
        monkeypatch.setattr(warehouse_api, '_floor3_log', fail)
    try:
        with TestClient(app) as client:
            _login(client, 'floor3-admin')
            with pytest.raises(RuntimeError, match='legacy-move-injected'):
                client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json=move_body(ids))
    finally:
        if fault == 'movement':
            event.remove(InventoryLocationMovement, 'before_insert', fail)
    assert snapshot(factory) == before
    assert ground_snapshot(factory) == ground_before


def test_move_permission_and_customer_scope_remain_current(merge_api):
    from app.models.access_control import UserPermissionOverride
    app, ids, factory = merge_api
    with factory() as db:
        rule = UserPermissionOverride(user_id=ids['scoped'], permission_code='warehouse.execute', is_allowed=False)
        db.add(rule)
        db.commit()
    with TestClient(app) as client:
        _login(client, 'floor3-scoped')
        before = snapshot(factory)
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json=move_body(ids))
        assert response.status_code == 403
        assert snapshot(factory) == before
        with factory() as db:
            db.scalar(select(UserPermissionOverride).where(UserPermissionOverride.user_id == ids['scoped'])).is_allowed = True
            db.scalar(select(InventoryPalletItem).where(
                InventoryPalletItem.pallet_id == ids['pallets'][0])).customer_id = ids['other']
            db.commit()
        before = snapshot(factory)
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json=move_body(ids))
        assert response.status_code == 403
        assert snapshot(factory) == before


def test_move_keeps_unflushed_internal_caller_fields(merge_api):
    from app.models.user import User
    from decimal import Decimal
    app, ids, factory = merge_api
    with factory(autoflush=False) as db:
        user = db.get(User, ids['admin'])
        lot = db.get(InventoryLot, ids['lots'][0])
        user.real_name = '内部调用待写字段'
        lot.remarks = '批次待写说明'
        lot.estimated_unit_cost_snapshot = Decimal('1.2345')
        lot.finished_detail.product_name_snapshot = '批次明细待写说明'
        result = floor3_locations.move_pallet(db, pallet_id=ids['pallets'][0], expected_version=1,
            to_location_id=ids['locations'][2], remarks=None, operator_id=ids['admin'],
            idempotency_key='move-internal-pending')
        assert result.pallet.version == 2 and lot.version == 2
        assert user.real_name == '内部调用待写字段'
        assert lot.remarks == '批次待写说明'
        assert lot.estimated_unit_cost_snapshot == Decimal('1.2345')
        assert lot.finished_detail.product_name_snapshot == '批次明细待写说明'
        db.commit()
    with factory() as db:
        assert db.get(User, ids['admin']).real_name == '内部调用待写字段'
        assert db.get(InventoryLot, ids['lots'][0]).remarks == '批次待写说明'
        assert db.get(InventoryLot, ids['lots'][0]).estimated_unit_cost_snapshot == Decimal('1.2345')
        assert db.get(InventoryLot, ids['lots'][0]).finished_detail.product_name_snapshot == '批次明细待写说明'


def test_finished_edit_internal_caller_continues_after_move(merge_api):
    from datetime import date
    app, ids, factory = merge_api
    with factory(autoflush=False) as db:
        edited = warehouse_inventory.edit_finished_lot(db, lot_id=ids['lots'][0], expected_version=1,
            is_general=False, customer_id=ids['tianhua'], product_id=ids['products'][0],
            quantity_available=8, location_id=ids['locations'][2], stock_date=date(2026, 7, 15),
            operator_id=ids['admin'], idempotency_key='legacy-edit-move')
        assert edited.version == 2 and edited.quantity_available == 8
        assert edited.pallet_item.pallet.location_id == ids['locations'][2]
        assert edited.pallet_item.quantity == 8
        db.commit()


def test_existing_current_status_contract_is_not_tightened(merge_api):
    app, ids, factory = merge_api
    with factory() as db:
        db.get(InventoryPallet, ids['pallets'][0]).status = 'closed'
        db.commit()
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json=move_body(ids))
        assert response.status_code == 200, response.text
        assert response.json()['pallet']['status'] == 'active'


def test_secondary_ground_slot_still_blocks_target_and_keeps_source_occupancy(merge_api):
    app, ids, factory = merge_api
    token = _ensure_location_layout_version(factory, ids['locations'][2])
    seed_ground_occupancy(factory, ids)
    with factory() as db:
        occupancy = db.scalar(select(WarehouseGroundOccupancy))
        occupancy.footprint_kind = 'double'
        db.add(WarehouseGroundOccupancySlot(occupancy_id=occupancy.id,
            location_id=ids['locations'][2], slot_sequence=2))
        db.commit()
    before, ground_before = snapshot(factory), ground_snapshot(factory)
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/move", json={
            **move_body(ids), 'expected_target_layout_version': token})
        assert response.status_code == 409, response.text
    assert snapshot(factory) == before and ground_snapshot(factory) == ground_before


def test_successful_move_releases_source_occupancy_once(merge_api):
    app, ids, factory = merge_api
    seed_ground_occupancy(factory, ids)
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        body = move_body(ids)
        url = f"/api/warehouse/pallets/{ids['pallets'][0]}/move"
        response = client.post(url, json=body)
        assert response.status_code == 200, response.text
        after = ground_snapshot(factory)
        assert after[0][0][1:3] == ('released', 2)
        assert after[1][0][1] == 'released'
        assert client.post(url, json=body).json()['idempotent_replay'] is True
        assert ground_snapshot(factory) == after

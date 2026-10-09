from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_floor3_locations_api import floor3_app, _login
from app.api import warehouse as warehouse_api
from app.models.audit import OperationLog
from app.models.material import Material
from app.models.order import OrderItem
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.warehouse_inventory import (
    InventoryLocationMovement, InventoryLot, InventoryMovement,
    InventoryPallet, InventoryPalletItem,
)
from app.services import floor3_locations, warehouse_inventory


def seed_cost_basis(db, ids):
    supplier = Supplier(standard_name='合并夹具供应商', normalized_name='合并夹具供应商',
                        is_active=True, version=1, sort_order=1)
    material = Material(code='A416D', supplier_name=supplier.standard_name,
        quote_price=Decimal('2.00'), price_unit='元/㎡', purchase_currency='CNY',
        purchase_tax_included=True, layer_count=5, flute_type='BE', is_active=True)
    db.add_all([supplier, material])
    db.flush()
    for pid in ids['products']:
        product = db.get(Product, pid)
        product.box_category = 'normal'
        product.default_material_code = 'A416D'
        product.material_id = material.id
        product.flute_type = 'BE'
        product.report_length_mm = product.base_report_length_mm = 800
        product.report_width_mm = product.base_report_width_mm = 200


@pytest.fixture()
def merge_api(floor3_app):
    app, ids, factory = floor3_app
    with factory() as db:
        seed_cost_basis(db, ids)
        first_item = db.scalar(select(OrderItem))
        first_item.requisition_status = '未报料'
        first_item.material_status = 'pending'
        second_item = OrderItem(order_id=first_item.order_id, product_id=ids['products'][2],
            quantity=10, unit_price=1, subtotal=10, requisition_status='未报料', material_status='pending',
            item_order_number='TM20260714001-002', snapshot_product_code='21301013',
            snapshot_product_name='纸箱3')
        db.add(second_item)
        db.flush()
        lots = []
        for index, quantity in ((0, 10), (1, 5)):
            lots.append(warehouse_inventory.manual_finished_in(db,
                customer_id=ids['tianhua'], product_id=ids['products'][index * 2],
                location_id=ids['locations'][index], quantity=quantity,
                stock_date=date(2026, 7, 14), source_type='manual', remarks=None,
                operator_id=ids['admin'], idempotency_key=f'legacy-merge-lot-{index}',
                pallet_code=f'LEGACY-MERGE-PALLET-{index}'))
        db.commit()
        ids = {**ids, 'lots': [lot.id for lot in lots],
               'pallets': [lot.pallet_item.pallet_id for lot in lots],
               'order_items': [first_item.id, second_item.id]}
    return app, ids, factory


def payload(factory, ids, key='legacy-merge-request'):
    with factory() as db:
        return dict(expected_version=db.get(InventoryPallet, ids['pallets'][0]).version,
            target_pallet_id=ids['pallets'][1],
            expected_target_version=db.get(InventoryPallet, ids['pallets'][1]).version,
            confirmed=True, idempotency_key=key)


def snapshot(factory):
    with factory() as db:
        return {
            'lots': [(lot.id, lot.version, lot.warehouse_location_id, lot.quantity_available,
                lot.quantity_reserved, lot.quantity_consumed, lot.quantity_damaged,
                lot.quantity_scrapped, lot.last_movement_at) for lot in db.scalars(select(InventoryLot).order_by(InventoryLot.id))],
            'pallets': [(p.id, p.version, p.location_id, p.status, p.is_current, p.closed_at)
                        for p in db.scalars(select(InventoryPallet).order_by(InventoryPallet.id))],
            'items': [(i.id, i.pallet_id, i.inventory_lot_id, i.customer_id, i.product_id, i.quantity)
                      for i in db.scalars(select(InventoryPalletItem).order_by(InventoryPalletItem.id))],
            'movements': [(m.id, m.movement_type, m.quantity, m.before_available, m.after_available,
                           m.before_reserved, m.after_reserved)
                          for m in db.scalars(select(InventoryMovement).order_by(InventoryMovement.id))],
            'location_movements': [m.id for m in db.scalars(select(InventoryLocationMovement).order_by(InventoryLocationMovement.id))],
            'business_audits': [a.id for a in db.scalars(select(OperationLog).where(OperationLog.event_category=='business').order_by(OperationLog.id))],
        }


def reserve(client, factory, ids, side, key):
    with factory() as db:
        version = db.get(InventoryLot, ids['lots'][side]).version
    response = client.post('/api/warehouse/finished/reservations', json=dict(
        order_item_id=ids['order_items'][side], inventory_lot_id=ids['lots'][side],
        quantity=2, expected_version=version, idempotency_key=key, warning_acknowledged_codes=[]))
    assert response.status_code == 200, response.text
    return response.json()['id']


def pause_after_initial_signature(monkeypatch, ids, interleave):
    original = floor3_locations._pallet_merge_signature
    called = False
    def hook(pallet):
        nonlocal called
        result = original(pallet)
        if not called and pallet.id == ids['pallets'][1]:
            called = True
            interleave()
        return result
    monkeypatch.setattr(floor3_locations, '_pallet_merge_signature', hook)


@pytest.mark.parametrize('side', [0, 1], ids=['source', 'target'])
@pytest.mark.parametrize('release', [False, True], ids=['reserve', 'reserve_release'])
def test_committed_reservation_drift_rejected_without_merge_writes(merge_api, monkeypatch, side, release):
    app, ids, factory = merge_api
    with TestClient(app) as merger, TestClient(app) as writer:
        _login(merger, 'floor3-admin')
        _login(writer, 'floor3-admin')
        body = payload(factory, ids)
        committed = {}
        def interleave():
            rid = reserve(writer, factory, ids, side, 'legacy-concurrent-reserve')
            if release:
                response = writer.post(f'/api/warehouse/reservations/{rid}/release', json={
                    'release_reason': '合并并发夹具', 'idempotency_key': 'legacy-concurrent-release'})
                assert response.status_code == 200, response.text
            committed.update(snapshot(factory))
        pause_after_initial_signature(monkeypatch, ids, interleave)
        response = merger.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all", json=body)
        assert response.status_code == 409, response.text
        assert snapshot(factory) == committed


@pytest.mark.parametrize('reserved', [False, True])
def test_fresh_merge_balances_version_and_exact_replay(merge_api, reserved):
    app, ids, factory = merge_api
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        if reserved:
            reserve(client, factory, ids, 0, 'legacy-existing-reserve')
        before = snapshot(factory)
        body = payload(factory, ids)
        url = f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all"
        response = client.post(url, json=body)
        assert response.status_code == 200, response.text
        assert response.json()['moved_item_count'] == 1
        after = snapshot(factory)
        assert after['lots'][0][1] == before['lots'][0][1] + 1
        assert after['lots'][0][3:8] == before['lots'][0][3:8]
        movement = after['movements'][-1]
        assert movement[1] == 'location_transfer'
        assert movement[3:5] == (before['lots'][0][3],) * 2
        assert movement[5:7] == (before['lots'][0][4],) * 2
        repeated = client.post(url, json=body)
        assert repeated.status_code == 200 and repeated.json()['idempotent_replay'] is True
        assert snapshot(factory) == after
        conflicting = client.post(url, json={**body, 'expected_version':body['expected_version']+1})
        assert conflicting.status_code == 409
        assert snapshot(factory) == after


@pytest.mark.parametrize('fault', ['movement', 'audit'])
def test_merge_runtime_fault_rolls_back_all_facts(merge_api, monkeypatch, fault):
    app, ids, factory = merge_api
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        before = snapshot(factory)
        body = payload(factory, ids)
        def fail(*args, **kwargs):
            raise RuntimeError('legacy-merge-injected')
        if fault == 'movement':
            monkeypatch.setattr(warehouse_inventory, 'record_location_transfer_without_quantity_change', fail)
        else:
            monkeypatch.setattr(warehouse_api, '_floor3_log', fail)
        with pytest.raises(RuntimeError, match='legacy-merge-injected'):
            client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all", json=body)
        assert snapshot(factory) == before


def test_current_scope_and_execute_permission_protect_merge(merge_api):
    from app.models.access_control import UserPermissionOverride
    app, ids, factory = merge_api
    with TestClient(app) as client:
        _login(client, 'floor3-scoped')
        with factory() as db:
            db.add(UserPermissionOverride(user_id=ids['scoped'], permission_code='warehouse.execute', is_allowed=True))
            db.get(InventoryPalletItem, db.scalar(select(InventoryPalletItem.id).where(
                InventoryPalletItem.pallet_id==ids['pallets'][1]))).customer_id=ids['other']
            db.commit()
        before = snapshot(factory)
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all", json=payload(factory,ids))
        assert response.status_code == 403
        assert snapshot(factory) == before
        with factory() as db:
            rule=db.scalar(select(UserPermissionOverride).where(UserPermissionOverride.user_id==ids['scoped']))
            rule.is_allowed=False
            db.commit()
        response = client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all", json=payload(factory,ids))
        assert response.status_code == 403
        assert snapshot(factory) == before


@pytest.mark.parametrize('change', ['member', 'quantity', 'location'])
def test_member_and_location_drift_rejected_after_lock(merge_api, monkeypatch, change):
    from app.models.warehouse_inventory import WarehouseLocation
    app, ids, factory = merge_api
    committed = {}
    def interleave():
        with factory() as db:
            item = db.scalar(select(InventoryPalletItem).where(InventoryPalletItem.pallet_id==ids['pallets'][0]))
            if change == 'member':
                item.product_id=ids['products'][1]
            elif change == 'quantity':
                item.quantity += 1
            else:
                db.get(WarehouseLocation, ids['locations'][1]).is_active=False
            db.commit()
        committed.update(snapshot(factory))
    with TestClient(app) as client:
        _login(client, 'floor3-admin')
        body=payload(factory,ids)
        pause_after_initial_signature(monkeypatch,ids,interleave)
        response=client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all",json=body)
        assert response.status_code == 409,response.text
        assert snapshot(factory) == committed


def test_same_key_completed_while_waiting_for_cas_still_replays(merge_api, monkeypatch):
    app, ids, factory = merge_api
    with TestClient(app) as first, TestClient(app) as second:
        _login(first,'floor3-admin')
        _login(second,'floor3-admin')
        url=f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all"
        body=payload(factory,ids)
        committed={}
        def interleave():
            response=second.post(url,json=body)
            assert response.status_code==200,response.text
            assert response.json()['idempotent_replay'] is False
            committed.update(snapshot(factory))
        pause_after_initial_signature(monkeypatch,ids,interleave)
        response=first.post(url,json=body)
        assert response.status_code==200,response.text
        assert response.json()['idempotent_replay'] is True
        assert snapshot(factory)==committed


def test_legacy_unlinked_pallet_member_can_still_merge(merge_api):
    app, ids, factory = merge_api
    with factory() as db:
        member=InventoryPalletItem(pallet_id=ids['pallets'][0], customer_id=ids['tianhua'],
            product_id=ids['products'][1], inventory_code='21301012', product_name='既有快照',
            item_type='finished',quantity=4,unit='boxes',match_status='matched')
        db.add(member)
        db.commit()
        member_id=member.id
    with TestClient(app) as client:
        _login(client,'floor3-admin')
        response=client.post(f"/api/warehouse/pallets/{ids['pallets'][0]}/merge-all",json=payload(factory,ids))
        assert response.status_code==200,response.text
        assert response.json()['moved_item_count']==2
    with factory() as db:
        member=db.get(InventoryPalletItem,member_id)
        assert member.inventory_lot_id is None and member.pallet_id==ids['pallets'][1]
        assert member.quantity==4


def test_existing_formal_lot_contract_with_complete_cost_basis(floor3_app):
    from test_floor3_locations_api import test_p1_16e2_merge_all_preserves_formal_lots_and_is_idempotent
    _, ids, factory = floor3_app
    with factory() as db:
        seed_cost_basis(db, ids)
        db.commit()
    test_p1_16e2_merge_all_preserves_formal_lots_and_is_idempotent(floor3_app)

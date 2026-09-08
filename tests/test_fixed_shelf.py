from datetime import date
from pathlib import Path
import importlib.util

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.api import fixed_shelf as api
from app.api.deps import get_db
from app.models.fixed_shelf import ShelfBinding, ShelfLotState, ShelfMutation, ShelfProfile
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
from app.services import fixed_shelf as shelf, location_candidates
from app.services.warehouse_inventory import manual_finished_in, transfer_finished_lot_between_locations, WarehouseInventoryError
from app.services.warehouse_rack_cells import sync_published_rack_cells
from test_p1_123_warehouse_region_rack_labels import rack_factory, _layout
from test_warehouse_inventory_foundation import seed_product, seed_other_customer_product
from test_warehouse_inventory_foundation import db as legacy_db
from test_n036_delivery_pick import pick_app


@pytest.fixture
def setup(rack_factory, monkeypatch):
    monkeypatch.setattr(location_candidates, 'load_warehouse_twin_published_floor_identity',
        lambda _: {'revision': 'rack-rev-1', 'zones_by_id': {'zone-fin-001': 'FIN-001'}})
    with rack_factory() as db:
        ids = sync_published_rack_cells(db, floor_layout=_layout(), operator_id=1).created_location_ids
        customer, product = seed_product(db)
        customer.chinese_short_name = '测试甲'
        db.commit()
        return rack_factory, product.id, customer.id, list(ids)


def configure(db, product_id, ids, units=20, version=0, capacity=None):
    from app.models.product import Product
    rows = []
    for i, lid in enumerate(ids):
        row = db.get(WarehouseLocation, lid)
        rows.append(dict(location_id=lid, priority=i, capacity=capacity,
            address_version=row.address_version, layout_version=row.floor3_layout.version))
    return shelf.save_profile(db, db.get(Product, product_id), expected_version=version, units_per_bundle=units, bindings=rows)


def incoming(db, pid, cid, lid, qty=125, key='incoming-1'):
    row = db.get(WarehouseLocation, lid)
    return manual_finished_in(db, customer_id=cid, product_id=pid, location_id=lid, quantity=qty,
        stock_date=date.today(), source_type='manual', remarks=None, operator_id=1, idempotency_key=key,
        expected_layout_version=row.floor3_layout.version)


def move(db, lot, lid, quantity):
    source = db.get(WarehouseLocation, lot.warehouse_location_id)
    target = db.get(WarehouseLocation, lid)
    return transfer_finished_lot_between_locations(db, lot_id=lot.id, expected_version=lot.version,
        quantity=quantity, location_id=lid, operator_id=1, idempotency_key='move-1',
        expected_source_location_id=source.id, expected_source_address_version=source.address_version,
        expected_source_layout_version=source.floor3_layout.version,
        expected_target_address_version=target.address_version, expected_target_layout_version=target.floor3_layout.version)


def test_incoming_target_partial_putaway_preserves_quantity_and_packaging(setup):
    factory, pid, cid, ids = setup
    with factory() as db:
        configure(db, pid, ids[:2])
        db.commit()
        lot = incoming(db, pid, cid, ids[2])
        db.commit()
        assert lot.warehouse_location_id == ids[2]
        assert db.get(ShelfLotState, lot.id).target_location_id == ids[0]
        configure(db, pid, ids[:2], units=25, version=1)
        db.commit()
        result = move(db, lot, ids[0], 100)
        db.commit()
        assert result.source_lot.quantity_available == 25
        assert result.target_lot.quantity_available == 100
        assert db.get(ShelfLotState, result.source_lot.id).target_location_id == ids[0]
        state = db.get(ShelfLotState, result.target_lot.id)
        assert state.target_location_id is None and state.units_per_bundle == 20
        groups = [{'lines': [{'lot_id': result.target_lot.id, 'pick_quantity': 85}]}]
        shelf.enrich_pick_groups(db, groups)
        assert groups[0]['lines'][0]['bundle_text'] == '4捆 + 5散只（20只/捆）'
        assert shelf.bundle_breakdown(100, None)['bundle_count'] is None


def test_bound_cell_rejects_mixed_goods_and_over_capacity(setup):
    factory, pid, cid, ids = setup
    with factory() as db:
        configure(db, pid, ids[:1], capacity=100)
        db.commit()
        with pytest.raises(WarehouseInventoryError, match='放不下'):
            incoming(db, pid, cid, ids[0], 101)
        db.rollback()
        assert db.scalar(select(InventoryLot.id)) is None
        other_customer, other_product = seed_other_customer_product(db, '02')
        db.commit()
        with pytest.raises(WarehouseInventoryError, match='混放'):
            incoming(db, other_product.id, other_customer.id, ids[0], 50)
        db.rollback()
        assert db.scalar(select(InventoryLot.id)) is None


def test_stale_configuration_and_occupied_unbinding_are_rejected(setup):
    factory, pid, cid, ids = setup
    with factory() as db:
        configure(db, pid, ids[:1])
        db.commit()
        with pytest.raises(shelf.ShelfError, match='配置已变化'):
            configure(db, pid, ids[:1], version=0)
        db.rollback()
        lot = incoming(db, pid, cid, ids[0], 80)
        db.commit()
        with pytest.raises(shelf.ShelfError, match='还有库存'):
            configure(db, pid, ids[1:2], version=1)
        db.rollback()
        assert db.get(ShelfBinding, ids[0]).product_id == pid
        assert db.get(ShelfProfile, pid).version == 1
        assert lot.quantity_available == 80


def test_unpublished_or_renamed_location_blocks_stale_write(setup):
    factory, pid, cid, ids = setup
    with factory() as db:
        row = db.get(WarehouseLocation, ids[0])
        row.is_active = False
        db.commit()
        with pytest.raises(shelf.ShelfError, match='停用'):
            configure(db, pid, ids[:1])
        db.rollback()
        assert db.get(ShelfProfile, pid) is None


def test_api_idempotency_packaging_and_read_only_permissions(setup):
    factory, pid, cid, ids = setup
    app = FastAPI()
    app.include_router(api.router, prefix='/shelf')
    def database():
        with factory() as db:
            yield db
    def admin():
        with factory() as db:
            return db.get(User, 1)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[api.can_read] = admin
    app.dependency_overrides[api.can_write] = admin
    with TestClient(app) as client:
        locations = client.get('/shelf/locations').json()['items']
        location = next(x for x in locations if x['location_id'] == ids[0])
        assert location['issue'] is None
        payload = dict(expected_version=0, units_per_bundle=20, idempotency_key='save-1', bindings=[dict(
            location_id=ids[0], priority=0, address_version=location['address_version'], layout_version=location['layout_version'])])
        first = client.put(f'/shelf/products/{pid}', json=payload)
        assert first.status_code == 200, first.text
        assert client.put(f'/shelf/products/{pid}', json=payload).json() == first.json()
        payload['units_per_bundle'] = 25
        assert client.put(f'/shelf/products/{pid}', json=payload).status_code == 409
        with factory() as db:
            assert len(db.scalars(select(ShelfMutation)).all()) == 1
            lot = incoming(db, pid, cid, ids[2])
            db.commit()
            lid, version = lot.id, lot.version
        response = client.put(f'/shelf/lots/{lid}/packaging', json=dict(expected_version=version, units_per_bundle=10, idempotency_key='pack-1'))
        assert response.status_code == 200, response.text
        listed = client.get(f'/shelf/products/{pid}/lots').json()['items']
        assert listed[0]['units_per_bundle'] == 10
        assert client.get('/shelf/putaway').json()['items'][0]['lot_id'] == lid
        del app.dependency_overrides[api.can_write]
        assert client.put(f'/shelf/products/{pid}', json=payload).status_code == 401


def test_migration_is_additive_and_refuses_loss_of_configuration(setup):
    factory, pid, cid, ids = setup
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/rs08v8x9z67_fixed_shelf.py'
    spec = importlib.util.spec_from_file_location('shelf_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with factory() as db:
        connection = db.connection()
        operations = Operations(MigrationContext.configure(connection))
        module.op = operations
        original = connection.exec_driver_sql('SELECT id, location_code FROM warehouse_locations ORDER BY id').all()
        module.downgrade()
        assert 'warehouse_shelf_profiles' not in inspect(connection).get_table_names()
        module.upgrade()
        assert connection.exec_driver_sql('SELECT id, location_code FROM warehouse_locations ORDER BY id').all() == original
        configure(db, pid, ids[:1])
        db.flush()
        with pytest.raises(RuntimeError, match='facts exist'):
            module.downgrade()


def test_reserved_transfer_copies_packaging_and_preserves_reserved_totals(legacy_db):
    from test_p1_25c2_staging_location_transfer import _case
    from app.services.warehouse_inventory import transfer_staging_finished_lot
    from app.models.warehouse_inventory import InventoryReservation
    db = legacy_db
    source, target, reservation = _case(db, available=40, reserved=60, suffix='shelf')
    db.add(ShelfLotState(lot_id=source.id, units_per_bundle=20))
    db.commit()
    result = transfer_staging_finished_lot(db, lot_id=source.id, expected_version=source.version,
        quantity=70, location_id=target.id, operator_id=None, idempotency_key='shelf-reserved-transfer',
        expected_target_layout_version=target.floor3_layout.version if target.floor3_layout else None)
    db.commit()
    assert result.source_lot.quantity_reserved + result.target_lot.quantity_reserved == 60
    assert result.source_lot.quantity_available + result.target_lot.quantity_available == 40
    rows = db.scalars(select(InventoryReservation).where(InventoryReservation.status != 'cancelled')).all()
    assert sum(x.reserved_stock_quantity - x.consumed_stock_quantity - x.released_stock_quantity for x in rows) == 60
    assert db.get(ShelfLotState, result.target_lot.id).units_per_bundle == 20


def test_customer_scope_blocks_search_labels_and_packaging(setup):
    factory, pid, cid, ids = setup
    from app.api.warehouse import _shelf_label_content
    from fastapi import HTTPException
    with factory() as db:
        configure(db, pid, ids[:1])
        lot = incoming(db, pid, cid, ids[2])
        user = User(username='shelf-scoped', password_hash='unused', role='sales',
            customer_access_mode='selected', real_name='隔离范围测试')
        db.add(user)
        db.commit()
        assert api.search_products(q='WH-M', customer_id=None, db=db, user=user)['items'] == []
        assert api.pending_putaway(db=db, user=user)['items'] == []
        with pytest.raises(HTTPException) as error:
            api.product_for_user(db, user, pid)
        assert error.value.status_code == 403
        with pytest.raises(HTTPException) as error:
            api.lot_for_user(db, user, lot.id)
        assert error.value.status_code == 403
        assert _shelf_label_content(db, db.get(WarehouseLocation, ids[0]), user) == {'restricted': True}


def test_stale_address_and_putaway_replay(setup):
    factory, pid, cid, ids = setup
    from starlette.requests import Request
    from fastapi import HTTPException
    with factory() as db:
        configure(db, pid, ids[:2])
        lot = incoming(db, pid, cid, ids[2])
        db.commit()
        source = shelf.location_info(db, db.get(WarehouseLocation, ids[2]))
        target = shelf.location_info(db, db.get(WarehouseLocation, ids[0]))
        payload = api.PutawayPayload(expected_version=lot.version, quantity=100, location_id=ids[0],
            address_version=target['address_version']+1, layout_version=target['layout_version'],
            source_location_id=ids[2], source_address_version=source['address_version'],
            source_layout_version=source['layout_version'], idempotency_key='putaway-1')
        request = Request({'type':'http', 'method':'POST', 'path':'/test', 'headers':[]})
        user = db.get(User, 1)
        with pytest.raises(HTTPException) as error:
            api.putaway(lot.id, payload, request, db, user)
        assert error.value.status_code == 409
        assert lot.quantity_available == 125
        assert db.get(ShelfMutation, 'putaway-1') is None
        payload.address_version = target['address_version']
        first = api.putaway(lot.id, payload, request, db, user)
        assert api.putaway(lot.id, payload, request, db, user) == first
        assert sum(x.quantity_available for x in db.scalars(select(InventoryLot)).all()) == 125


def test_frontend_javascript_parses(tmp_path):
    import re, shutil, subprocess
    root = Path(__file__).resolve().parents[1]
    for name in ('fixed-shelf.html', 'location-label.html', 'delivery-pick-print.html', 'mobile_delivery_pick.html'):
        for i, script in enumerate(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', (root/'static'/name).read_text(encoding='utf-8'), re.S)):
            if not script.strip():
                continue
            target = tmp_path / f'{name}-{i}.js'
            target.write_text(script, encoding='utf-8')
            result = subprocess.run([shutil.which('node'), '--check', str(target)], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr


def test_mm_display_is_integer_and_does_not_change_other_specification_values():
    assert shelf.display_specification('800.00 × 200.00 × 100.00 mm') == '800 × 200 × 100 mm'
    assert shelf.display_specification('800.5*200.1*100.0') == '801*200*100'
    assert shelf.display_specification('厚度 2.5 mm / 材质 K2.5 / 料号 A800.00') == '厚度 3 mm / 材质 K2.5 / 料号 A800.00'
    assert shelf.integer_mm(200) == '200'


def test_same_fixed_shelf_order_row_cannot_be_promised_to_two_pending_tasks(pick_app):
    from test_n036_delivery_pick import _login, _create_task
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem
    app, factory, ids, _ = pick_app
    with factory() as db:
        row = db.get(OrderItem, ids['order_items'][0])
        db.add(ShelfProfile(product_id=row.product_id, units_per_bundle=20, version=1))
        second = Delivery(delivery_number='SHELF-SECOND', customer_id=ids['customer'], delivery_date=date.today(), status='pending', total_quantity=10)
        db.add(second)
        db.flush()
        db.add(DeliveryItem(delivery_id=second.id, order_item_id=row.id, delivered_quantity=10))
        db.commit()
        second_id = second.id
    with TestClient(app) as client:
        _login(client, 'admin')
        first = _create_task(client, ids['delivery'])
        response = client.post(f'/api/deliveries/{second_id}/pick-task')
        assert response.status_code == 409 and '重复' in response.json()['detail']
        assert _create_task(client, ids['delivery'])['id'] == first['id']

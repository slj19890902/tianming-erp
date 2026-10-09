"""Real order API regressions for chain entry boundaries; disposable data only."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from tests.test_phase5_orders import order_api_app, _login
from tests.test_semi_finished_order_reservation import b1_app, fictional_document_evidence
from tests.test_order_inventory_reliability import composite_requisition_app, _p181_published_map_identity


@pytest.fixture
def chain_app(order_api_app):
    app, factory = order_api_app
    from app.core.config import settings
    app.state.erp_settings = settings
    with factory() as db:
        for product in db.scalars(select(Product)):
            product.unit = "只"
        db.commit()
    return app, factory


def payload():
    return dict(customer_id=1, customer_po="CHAIN-AUDIT-FICTIONAL",
                items=[dict(product_id=1, quantity=20, unit_price="3.60")])


def assert_empty(factory):
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 0


@pytest.mark.parametrize("status", ["delivered", "completed", "production", "pending_delivery", "cancelled", "not-a-status"])
def test_new_order_cannot_forge_downstream_status(chain_app, status):
    app, factory = chain_app
    with TestClient(app) as client:
        _login(client)
        result = client.post('/api/orders', json={**payload(), "status": status})
        assert result.status_code in (400, 422), result.text
    assert_empty(factory)


@pytest.mark.parametrize("payment_status", ["paid", "unknown"])
def test_new_order_cannot_forge_receipt_of_payment(chain_app, payment_status):
    app, factory = chain_app
    with TestClient(app) as client:
        _login(client)
        result = client.post('/api/orders', json={**payload(), "payment_status": payment_status})
        assert result.status_code in (400, 422), result.text
    assert_empty(factory)


@pytest.mark.parametrize("field,value", [("is_active", False), ("status", "inactive")])
def test_manual_new_order_rechecks_customer_active(chain_app, field, value):
    app, factory = chain_app
    with factory() as db:
        setattr(db.get(Customer, 1), field, value)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        result = client.post('/api/orders', json=payload())
        assert result.status_code == 400, result.text
        assert '停用' in result.text
    assert_empty(factory)


@pytest.mark.parametrize("state", ["cancelled", "closed", "force_closed"])
def test_closed_order_line_cannot_be_edited(chain_app, state):
    app, factory = chain_app
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/orders', json=payload())
        assert created.status_code == 201, created.text
        item = created.json()['items'][0]
        with factory() as db:
            if state == "force_closed":
                db.get(OrderItem, item['id']).is_force_closed = True
            else:
                db.get(Order, created.json()['id']).status = state
            db.commit()
        result = client.put('/api/orders/items/'+str(item['id']), json=dict(
            quantity=21, unit_price="3.60", product_code=item['snapshot_product_code'],
            product_name=item['snapshot_product_name'], material=item['snapshot_material'],
            specification=item['snapshot_spec']))
        assert result.status_code == 409, result.text
    with factory() as db:
        assert db.get(OrderItem, item['id']).quantity == 20


@pytest.mark.parametrize("quantity", [True, "NaN", "Infinity", 1e30])
def test_invalid_quantity_fails_cleanly_without_order(chain_app, quantity):
    app, factory = chain_app
    data = payload()
    data['items'][0]['quantity'] = quantity
    with TestClient(app) as client:
        _login(client)
        result = client.post('/api/orders', json=data)
        assert result.status_code in (400, 422), result.text
    assert_empty(factory)


def test_order_create_replay_and_changed_payload_are_atomic(chain_app):
    app, factory = chain_app
    data = {**payload(), "idempotency_key": "chain-audit-replay-1"}
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/orders', json=data)
        assert created.status_code == 201, created.text
        replay = client.post('/api/orders', json=data)
        assert replay.status_code == 201 and replay.json()['id'] == created.json()['id']
        changed = deepcopy(data)
        changed['items'][0]['quantity'] = 30
        assert client.post('/api/orders', json=changed).status_code == 409
        with factory() as db:
            db.get(Customer, 1).is_active = False
            db.commit()
        replay = client.post('/api/orders', json=data)
        assert replay.status_code == 201 and replay.json()['id'] == created.json()['id']
        readback = client.get('/api/orders/create-attempts/'+data['idempotency_key'])
        assert readback.status_code == 200 and readback.json()['status'] == 'completed'
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(OrderItem.quantity)) == 20


@pytest.mark.parametrize('initial_status', ['pending_confirmation', 'pending_production'])
def test_normal_initial_status_and_edit_remain_supported(chain_app, initial_status):
    app, factory = chain_app
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/orders', json={**payload(), 'status': initial_status})
        assert created.status_code == 201, created.text
        item = created.json()['items'][0]
        edit = dict(quantity=21, unit_price='3.60', product_code=item['snapshot_product_code'],
                    product_name=item['snapshot_product_name'], material=item['snapshot_material'],
                    specification=item['snapshot_spec'])
        for invalid in (True, 2147483648):
            result = client.put('/api/orders/items/'+str(item['id']), json={**edit, 'quantity':invalid})
            assert result.status_code in (400, 422), result.text
        result = client.put('/api/orders/items/'+str(item['id']), json=edit)
        assert result.status_code == 200 and result.json()['quantity'] == 21, result.text


def test_later_invalid_line_rolls_back_all_created_business_facts(chain_app):
    from app.models.audit import OperationLog
    from app.models.production import ProductionTask
    app, factory = chain_app
    data = {**payload(), 'idempotency_key':'chain-atomic-invalid-line'}
    data['items'].append(dict(product_id=2, quantity=10, unit_price='-1'))
    with TestClient(app) as client:
        _login(client)
        result = client.post('/api/orders', json=data)
        assert result.status_code == 400, result.text
    assert_empty(factory)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ProductionTask)) == 0
        assert db.scalar(select(func.count()).select_from(OperationLog).where(
            OperationLog.action == 'order_create_replay')) == 0


def test_different_import_files_without_client_key_have_distinct_identity(b1_app):
    from tests.test_t02_order_import_source_identity import _ready_product, _payload
    from tests.test_semi_finished_order_reservation import login
    app, factory = b1_app
    _ready_product(factory)
    with factory() as db:
        for product in db.scalars(select(Product)):
            product.unit = "只"
        db.commit()
    first = _payload(app, "1" * 64, "discarded-key-1")
    second = _payload(app, "2" * 64, "discarded-key-2")
    for data in (first, second):
        data.pop("idempotency_key")
    with TestClient(app) as client:
        login(client, "sales")
        saved = client.post('/api/orders', json=first)
        assert saved.status_code == 201, saved.text
        other = client.post('/api/orders', json=second)
        assert other.status_code == 201, other.text
        assert other.json()['id'] != saved.json()['id']
        replay = client.post('/api/orders', json=first)
        assert replay.status_code == 201 and replay.json()['id'] == saved.json()['id']
        assert replay.json()['source_replay'] is True
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 2


@pytest.mark.parametrize('line_quantities', [(50,), (50, 30, 30)])
def test_exact_sheet_stock_keeps_matching_after_order_identity_is_created(b1_app, line_quantities):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from tests.test_semi_finished_order_reservation import add_semi_lot, semi_plan, order_item, post_order, login
    app, factory = b1_app
    lot_id, version = add_semi_lot(factory, quantity=100, key='chain-physical-signature', allowed_product_ids=[1,2,3])
    items = [order_item(index, quantity, {'semi':[semi_plan(lot_id, version, quantity)]}, line=f'line-{index}')
             for index, quantity in enumerate(line_quantities, 1)]
    with TestClient(app) as client:
        login(client)
        result = post_order(client, items, 'CHAIN-PHYSICAL-SIGNATURE')
        assert result.status_code == 201, result.text
    with factory() as db:
        amounts = list(db.scalars(select(InventoryReservation.credited_requirement_quantity)
                                  .where(InventoryReservation.reservation_type == 'semi_order')
                                  .order_by(InventoryReservation.order_item_id)))
        assert amounts == ([50] if len(line_quantities) == 1 else [50, 30, 20])
        lot = db.get(InventoryLot, lot_id)
        assert lot.quantity_reserved == sum(amounts)
        assert lot.quantity_available + lot.quantity_reserved == 100


def test_different_physical_sheet_signature_is_still_rejected(b1_app):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from tests.test_semi_finished_order_reservation import add_semi_lot, semi_plan, order_item, post_order, login
    app, factory = b1_app
    lot_id, version = add_semi_lot(factory, quantity=100, key='chain-different-signature', width=610)
    with TestClient(app) as client:
        login(client)
        result = post_order(client, [order_item(1, 50, {'semi':[semi_plan(lot_id, version, 50)]})],
                            'CHAIN-DIFFERENT-SIGNATURE')
        assert result.status_code == 409, result.text
    assert_empty(factory)
    with factory() as db:
        assert db.get(InventoryLot, lot_id).quantity_available == 100
        assert db.scalar(select(func.count()).select_from(InventoryReservation)) == 0


def test_signature_keeps_frozen_processed_order_context(composite_requisition_app, _p181_published_map_identity):
    from dataclasses import replace
    from app.models.warehouse_inventory import InventoryLot
    from app.services.semi_finished_inventory import SemiFinishedSignature
    from app.services.processed_sheet_matching import processed_match
    from tests.test_order_inventory_reliability import seed_physical_graph, processed_lots
    app, factory = composite_requisition_app
    seed_physical_graph(factory)
    ids = processed_lots(factory)
    with factory() as db:
        product = db.get(Product, 2)
        expected = SemiFinishedSignature(customer_id=1, board_length_mm=product.report_length_mm,
            board_width_mm=product.report_width_mm, normalized_material_code=product.material.code,
            flute_type=product.flute_type, component_type='whole', pieces_per_box=1,
            stock_yield_per_sheet=1, order_item_id=1)
        product.production_notes = '后来修改主档，不得覆盖订单冻结的实际工艺'
        db.flush()
        lot = db.get(InventoryLot, ids[0])
        assert processed_match(db, lot, product, expected)['known'] is True
        assert processed_match(db, lot, product, replace(expected, order_item_id=None))['known'] is False

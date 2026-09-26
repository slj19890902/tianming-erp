import json
from types import SimpleNamespace
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_delivery_physical_quantities import seed_physical, payload
from tests.test_p1_15b_unordered_finished_delivery import unordered_finished_delivery_app, _login
from test_p1_140_external_stock_replenishment import external_stock_app, _seed_external_warning, _login as external_login
from test_p1_40a_packaging_masterdata import p1_40a_app


def _dispatched_warning(app, factory, *, split=False):
    from app.models.stock_replenishment import InventoryStockPolicy
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    with factory() as db:
        policy = InventoryStockPolicy(policy_name='预警门禁', target_inventory_type='finished',
            customer_id=customer_id, product_id=product_id, warning_quantity=100, target_quantity=201)
        db.add(policy)
        db.commit()
        policy_id = policy.id
    ids = []
    with TestClient(app) as client:
        _login(client)
        for quantity in ([50, 50] if split else [100]):
            created = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, quantity))
            assert created.status_code == 201, created.text
            delivery_id = created.json()['id']
            assert client.put(f'/api/deliveries/{delivery_id}/dispatch').status_code == 200
            ids.append(delivery_id)
    return ids, policy_id, lot_id


@pytest.mark.parametrize('gate', ['warehouse.view', 'requisition.execute', 'deliveries.execute',
                                 'customer_scope', 'cancelled', 'inactive'])
def test_warning_confirmation_gates_leave_no_draft(unordered_finished_delivery_app, gate):
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot
    app, factory = unordered_finished_delivery_app
    ids, policy_id, lot_id = _dispatched_warning(app, factory)
    endpoint = f'/api/deliveries/{ids[0]}/stock-warnings'
    with TestClient(app) as client:
        _login(client)
        assert client.get(endpoint).status_code == 200
        if gate == 'cancelled':
            assert client.put(f'/api/deliveries/{ids[0]}/cancel').status_code == 200
        with factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            if gate in {'warehouse.view', 'requisition.execute', 'deliveries.execute', 'customer_scope'}:
                user.role = 'sales'
                for permission in ['deliveries.view', 'deliveries.execute', 'warehouse.view', 'requisition.execute']:
                    db.add(UserPermissionOverride(user_id=user.id, permission_code=permission,
                        is_allowed=permission != gate))
                if gate == 'customer_scope':
                    user.customer_access_mode = 'selected'
            elif gate == 'inactive':
                db.get(InventoryStockPolicy, policy_id).active = False
            db.commit()
        rejected = client.post(endpoint + '/confirm', json={'items': [
            {'policy_id': policy_id, 'physical_quantity': 201}]})
        assert rejected.status_code == (409 if gate in {'cancelled', 'inactive'} else 403), rejected.text
    with factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 0
        assert db.get(InventoryLot, lot_id).quantity_available == (200 if gate == 'cancelled' else 0)


def test_warning_confirmation_audit_failure_rolls_back(unordered_finished_delivery_app, monkeypatch):
    from app.api import deliveries
    from app.models.stock_replenishment import StockReplenishmentOrder
    app, factory = unordered_finished_delivery_app
    ids, policy_id, _ = _dispatched_warning(app, factory)
    endpoint = f'/api/deliveries/{ids[0]}/stock-warnings/confirm'
    request = {'items': [{'policy_id': policy_id, 'physical_quantity': 201}]}
    original = deliveries.append_audit_event
    def fail_audit(*args, **kwargs):
        if kwargs.get('action_code') == 'create_delivery_warning_draft':
            raise RuntimeError('injected warning audit failure')
        return original(*args, **kwargs)
    with TestClient(app) as client:
        _login(client)
        monkeypatch.setattr(deliveries, 'append_audit_event', fail_audit)
        with pytest.raises(RuntimeError, match='injected warning audit failure'):
            client.post(endpoint, json=request)
        with factory() as db:
            assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 0
        monkeypatch.setattr(deliveries, 'append_audit_event', original)
        retry = client.post(endpoint, json=request)
        assert retry.status_code == 200 and retry.json()['orders'][0]['created'], retry.text


@pytest.mark.parametrize('same_delivery', [True, False])
def test_concurrent_warning_confirmation_does_not_double_cover(unordered_finished_delivery_app, same_delivery):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    app, factory = unordered_finished_delivery_app
    ids, policy_id, _ = _dispatched_warning(app, factory, split=True)
    barrier = Barrier(2)
    def confirm(delivery_id):
        with TestClient(app) as client:
            _login(client)
            barrier.wait(timeout=15)
            return client.post(f'/api/deliveries/{delivery_id}/stock-warnings/confirm', json={
                'items': [{'policy_id': policy_id, 'physical_quantity': 201}]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(confirm, ids[0])
        second = pool.submit(confirm, ids[0] if same_delivery else ids[1])
        responses = [first.result(timeout=30), second.result(timeout=30)]
    assert sorted(r.status_code for r in responses) == ([200, 200] if same_delivery else [200, 409]), [r.text for r in responses]
    if same_delivery:
        assert sorted(r.json()['orders'][0]['created'] for r in responses) == [False, True]
    with factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 1
        assert db.scalar(select(func.sum(StockReplenishmentOrderItem.quantity))) == 201


def test_paperboard_dispatch_warning_freezes_complete_plan(unordered_finished_delivery_app):
    from app.models.product import Product
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200, external=False)
    with factory() as db:
        product = db.get(Product, product_id)
        product.box_style = '模切内盒'
        product.flute_type = 'B'
        product.crease_type = '净料'
        product.default_cutting_mode = '一开二'
        policy = InventoryStockPolicy(policy_name='纸板补库', target_inventory_type='finished',
            customer_id=customer_id, product_id=product_id, warning_quantity=150, target_quantity=250)
        db.add(policy)
        db.commit()
        policy_id = policy.id
    with TestClient(app) as client:
        _login(client)
        request = payload(customer_id, product_id, lot_id, 50)
        request['lines'][0]['allocations'][0]['quantity'] = 50
        created = client.post('/api/deliveries', json=request)
        assert created.status_code == 201, created.text
        delivery_id = created.json()['id']
        assert client.put(f'/api/deliveries/{delivery_id}/dispatch').status_code == 200
        endpoint = f'/api/deliveries/{delivery_id}/stock-warnings'
        warning = client.get(endpoint)
        assert warning.status_code == 200, warning.text
        row = warning.json()['items'][0]
        assert row['proposed_items'][0]['quantity'] == 50
        with factory() as db:
            db.get(Product, product_id).report_width_mm = 190
            db.commit()
        stale = client.post(endpoint + '/confirm', json={'items': [{'policy_id': policy_id, 'plan_hash': row['plan_hash']}]})
        assert stale.status_code == 409, stale.text
        updated = client.get(endpoint).json()['items'][0]
        accepted = client.post(endpoint + '/confirm', json={'items': [{'policy_id': policy_id, 'plan_hash': updated['plan_hash']}]})
        assert accepted.status_code == 200, accepted.text
        repeated = client.post(endpoint + '/confirm', json={'items': [{'policy_id': policy_id, 'plan_hash': updated['plan_hash']}]})
        assert repeated.status_code == 200 and repeated.json()['orders'][0]['created'] is False
        assert client.get(endpoint).json()['items'][0]['proposed_items'] == []
    with factory() as db:
        order = db.scalar(select(StockReplenishmentOrder))
        assert order.status == 'draft' and len(order.items) == 1
        assert order.items[0].quantity == 50 and order.items[0].report_width_mm == 190
        assert order.items[0].quantity_contract_json is None


def test_dispatch_warning_confirmation_rechecks_gap_and_pending_drafts(unordered_finished_delivery_app):
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseBatch
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    with factory() as db:
        policy = InventoryStockPolicy(policy_name='发货补库', target_inventory_type='finished',
            customer_id=customer_id, product_id=product_id, warning_quantity=100, target_quantity=201)
        db.add(policy)
        db.commit()
        policy_id = policy.id
    with TestClient(app) as client:
        _login(client)
        first = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 50))
        assert first.status_code == 201, first.text
        delivery_id = first.json()['id']
        endpoint = f'/api/deliveries/{delivery_id}/stock-warnings/confirm'
        request = {'items': [{'policy_id': policy_id, 'physical_quantity': 101}]}
        blocked = client.post(endpoint, json=request)
        assert blocked.status_code == 409, blocked.text
        assert client.put(f'/api/deliveries/{delivery_id}/dispatch').status_code == 200
        stale = client.post(endpoint, json={'items': [{'policy_id': policy_id, 'physical_quantity': 100}]})
        assert stale.status_code == 409, stale.text
        with factory() as db:
            assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 0
        confirmed = client.post(endpoint, json=request)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()['orders'][0]['created'] is True
        repeated = client.post(endpoint, json=request)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['orders'][0]['id'] == confirmed.json()['orders'][0]['id']
        assert repeated.json()['orders'][0]['created'] is False
        warning = client.get(f'/api/deliveries/{delivery_id}/stock-warnings').json()['items'][0]
        assert warning['pending_physical_quantity'] == 101
        assert warning['suggested_physical_quantity'] == 0
        second = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 50))
        assert second.status_code == 201, second.text
        second_id = second.json()['id']
        assert client.put(f'/api/deliveries/{second_id}/dispatch').status_code == 200
        warning = client.get(f'/api/deliveries/{second_id}/stock-warnings').json()['items'][0]
        assert warning['pending_physical_quantity'] == 101
        assert warning['suggested_physical_quantity'] == 100
        another = client.post(f'/api/deliveries/{second_id}/stock-warnings/confirm',
            json={'items': [{'policy_id': policy_id, 'physical_quantity': 100}]})
        assert another.status_code == 200, another.text
        with factory() as db:
            duplicate = InventoryStockPolicy(policy_name='历史重复策略', target_inventory_type='finished',
                customer_id=customer_id, product_id=product_id, warning_quantity=100, target_quantity=201)
            db.add(duplicate)
            db.commit()
            duplicate_id = duplicate.id
        rejected = client.post(endpoint, json={'items': [
            {'policy_id': policy_id, 'physical_quantity': 101},
            {'policy_id': duplicate_id, 'physical_quantity': 101}]})
        assert rejected.status_code == 409 and '重复库存预警' in rejected.json()['detail']
    with factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 2
        assert db.scalar(select(func.count(ExternalPackagingPurchaseBatch.id))) == 0
        assert db.get(InventoryLot, lot_id).quantity_available == 0


def test_warning_draft_preserves_odd_piece_and_replays_frozen_snapshot(unordered_finished_delivery_app):
    from app.models.delivery import Delivery
    from app.models.product import Product
    from app.models.user import User
    from app.models.order import Order
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseBatch
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryMovement
    from app.services.stock_warning_drafts import create_external_warning_draft
    from app.services.unified_procurement import pending_stock_rows

    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 100))
        assert created.status_code == 201, created.text
        delivery_id = created.json()['id']
        sent = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert sent.status_code == 200, sent.text
    with factory() as db:
        user = db.scalar(select(User).where(User.username == 'admin'))
        policy = InventoryStockPolicy(policy_name='余片补库', target_inventory_type='finished',
            customer_id=customer_id, product_id=product_id, warning_quantity=0, target_quantity=1)
        db.add(policy)
        db.flush()
        before = [db.scalar(select(func.count(model.id))) for model in
                  (Order, ExternalPackagingPurchaseBatch, InventoryMovement)]
        draft, fresh = create_external_warning_draft(db, policy=policy,
            delivery=db.get(Delivery, delivery_id), physical_quantity=1, operator_id=user.id)
        assert fresh and draft.status == 'draft'
        assert draft.confirmed_at is None and draft.confirmed_by is None
        assert draft.items[0].quantity == 1
        contract = json.loads(draft.items[0].quantity_contract_json)
        assert contract['physical_quantity'] == 1 and contract['physical_basis'] == 2
        assert contract['physical_unit'] == '片' and contract['customer_unit'] == '只'
        draft_id, policy_id, user_id = draft.id, policy.id, user.id
        db.commit()
    # New session simulates refresh/restart, changed master and changed recommendation.
    with factory() as db:
        db.get(Product, product_id).external_packaging_default_purchase_quantity_basis = 7
        db.flush()
        repeated, fresh = create_external_warning_draft(db,
            policy=db.get(InventoryStockPolicy, policy_id), delivery=db.get(Delivery, delivery_id),
            physical_quantity=20, operator_id=user_id)
        assert not fresh and repeated.id == draft_id
        assert repeated.items[0].quantity == 1
        assert json.loads(repeated.items[0].quantity_contract_json) == contract
        assert [db.scalar(select(func.count(model.id))) for model in
                (Order, ExternalPackagingPurchaseBatch, InventoryMovement)] == before
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 1
        rows = pending_stock_rows(db, db.get(User, user_id))
        row = next(row for row in rows if row['source_id'] == draft_id)
        assert row['procurement_mode'] == 'external_purchase'
        assert row['quantity'] == 1 and row['external_purchase_quantity'] == '1'
        assert row['unit'] == '片'


@pytest.mark.parametrize('quantity,receipts', [(1, [1]), (3, [1, 2])])
def test_physical_warning_draft_purchase_and_receipt(external_stock_app, quantity, receipts):
    from app.models.product import Product
    from app.models.user import User
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingPurchaseBatch
    from app.models.warehouse_inventory import WarehouseLocation, InventoryLot
    from app.services.stock_warning_drafts import create_external_warning_draft, confirm_external_warning_draft
    from app.services.unified_procurement import pending_stock_rows
    policy_id, product_id = _seed_external_warning(external_stock_app)
    with external_stock_app.state.factory() as db:
        policy = db.get(InventoryStockPolicy, policy_id)
        user = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
        db.add(WarehouseLocation(location_code='F1-DISPATCH-01', location_name='一楼成品待送区',
            warehouse_type='finished', warehouse_floor=1, area_code='DISPATCH',
            storage_type='temporary_aisle', source_version='P1-25C', placement_status='placed', is_active=True))
        order, _ = create_external_warning_draft(db, policy=policy,
            delivery=SimpleNamespace(id=901, status='dispatched', is_historical_backfill=False,
                customer_id=policy.customer_id), physical_quantity=quantity, operator_id=user.id)
        order_id, user_id, request_hash = order.id, user.id, order.request_hash
        db.commit()
    with external_stock_app.state.factory() as db:
        # Purchase uses the saved 1:2 ratio despite a later master change.
        db.get(Product, product_id).external_packaging_default_purchase_quantity_basis = 7
        db.commit()
    with TestClient(external_stock_app) as client:
        external_login(client)
        endpoint = f'/api/requisition/stock-replenishment/orders/{order_id}/external-purchase'
        preview = client.get(endpoint + '-preview')
        assert preview.status_code == 200, preview.text
        reviewed = {'expected_request_hash':request_hash, 'expected_quote_hash':preview.json()['expected_quote_hash']}
        stale = client.post(endpoint, json={**reviewed, 'expected_request_hash': '0' * 64})
        assert stale.status_code == 409, stale.text
        from app.models.supplier import Supplier
        with external_stock_app.state.factory() as db:
            supplier = db.scalar(select(Supplier).where(Supplier.display_name == preview.json()['supplier_name']))
            assert supplier is not None
            supplier.display_name += '（名称更新）'
            db.commit()
        changed = client.post(endpoint, json=reviewed)
        assert changed.status_code == 409, changed.text
        refreshed = client.get(endpoint + '-preview')
        assert refreshed.status_code == 200, refreshed.text
        reviewed['expected_quote_hash'] = refreshed.json()['expected_quote_hash']
        confirmed = client.post(endpoint, json=reviewed)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()['created'] is True
        assert confirmed.json()['request_hash'] == request_hash
        assert confirmed.json()['items'][0]['quantity_contract']['physical_quantity'] == quantity
        repeated = client.post(endpoint, json=reviewed)
        assert repeated.status_code == 200 and repeated.json()['created'] is False
    with external_stock_app.state.factory() as db:
        order = db.get(StockReplenishmentOrder, order_id)
        user = db.get(User, user_id)
        assert order.status == 'confirmed'
        purchased = db.scalar(select(ExternalPackagingPurchaseItem))
        assert purchased.purchase_quantity == quantity
        assert purchased.purchase_quantity_basis_snapshot == 2
        assert purchased.order_quantity_basis_snapshot == 1
        purchase_id, item_id = purchased.purchase_order_id, purchased.id
        assert not any(row['source_id'] == order_id for row in pending_stock_rows(db, user))
        db.commit()
    with external_stock_app.state.factory() as db:
        order, fresh = confirm_external_warning_draft(db, order=db.get(StockReplenishmentOrder, order_id),
            operator=db.get(User, user_id))
        assert not fresh and db.scalar(select(func.count(ExternalPackagingPurchaseBatch.id))) == 1
    with TestClient(external_stock_app) as client:
        external_login(client)
        received_total = 0
        for index, received in enumerate(receipts):
            body = dict(idempotency_key=f'physical-draft-receive-{index}',
                lines=[dict(purchase_item_id=item_id, received_quantity=str(received))])
            response = client.post(f'/api/external-packaging-purchases/{purchase_id}/receipts', json=body)
            assert response.status_code == 200, response.text
            repeated = client.post(f'/api/external-packaging-purchases/{purchase_id}/receipts', json=body)
            assert repeated.status_code == 200 and repeated.json()['created'] is False
            received_total += received
            with external_stock_app.state.factory() as db:
                order = db.get(StockReplenishmentOrder, order_id)
                assert order.items[0].stocked_quantity == received_total
                assert order.status == ('stocked' if received_total == quantity else 'partially_stocked')
                assert db.scalar(select(func.sum(InventoryLot.quantity_available))) == received_total

from fastapi.testclient import TestClient
from sqlalchemy import select, func
from tests.test_phase7_deliveries import delivery_api_app, _create_payload, _login, _seed_historical_finished_delivery_inventory
from tests.test_p1_15b_unordered_finished_delivery import unordered_finished_delivery_app, _seed, _unordered_payload, _create_unordered_delivery, _login as unordered_login, _stock_snapshot
from app.models.delivery import DeliveryItem
from app.models.warehouse_inventory import InventoryMovement, DeliveryInventoryAllocation


def test_save_failure_rolls_back_revision_and_reports_constraint(delivery_api_app, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from app.api import deliveries
    from app.models.delivery import Delivery
    app, factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, 'admin')
        created = client.post('/api/deliveries', json=_create_payload())
        assert created.status_code == 201, created.text
        did = created.json()['id']
        with factory() as db:
            old_ids = list(db.scalars(select(DeliveryItem.id).where(DeliveryItem.delivery_id == did)))
            version = db.get(Delivery, did).version
        def fail(*args, **kwargs):
            raise IntegrityError('test', {}, Exception('FOREIGN KEY constraint failed'))
        monkeypatch.setattr(deliveries, '_write_audit', fail)
        saved = client.put(f'/api/deliveries/{did}', json=_create_payload())
        assert saved.status_code == 409
        assert '历史记录发生冲突' in saved.json()['detail']
        with factory() as db:
            assert db.get(Delivery, did).version == version
            assert list(db.scalars(select(DeliveryItem.id).where(DeliveryItem.delivery_id == did))) == old_ids
            assert all(db.get(DeliveryItem, oid).is_current for oid in old_ids)


def test_cancel_then_add_order_line_preserves_history_and_only_dispatches_once(delivery_api_app):
    app, factory = delivery_api_app
    _seed_historical_finished_delivery_inventory(factory, 1, 2)
    with TestClient(app) as client:
        _login(client, 'admin')
        payload = _create_payload()
        payload['items'] = payload['items'][:1]
        created = client.post('/api/deliveries', json=payload)
        assert created.status_code == 201, created.text
        did = created.json()['id']
        assert client.put(f'/api/deliveries/{did}/dispatch').status_code == 200
        cancelled = client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            old_ids = list(db.scalars(select(DeliveryItem.id).where(DeliveryItem.delivery_id == did)))
            movements = db.scalar(select(func.count()).select_from(InventoryMovement))
        payload = _create_payload()
        payload['expected_version'] = cancelled.json()['version']
        saved = client.put(f'/api/deliveries/{did}', json=payload)
        assert saved.status_code == 200, saved.text
        assert len(saved.json()['items']) == 2
        assert saved.json()['total_quantity'] == 70
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(InventoryMovement)) == movements
            assert all(db.get(DeliveryItem, oid).is_current is False for oid in old_ids)
            assert db.scalar(select(func.count()).select_from(DeliveryInventoryAllocation).where(DeliveryInventoryAllocation.delivery_item_id.in_(old_ids))) > 0
        stale = client.put(f'/api/deliveries/{did}', json=payload)
        assert stale.status_code == 409
        assert stale.json()['detail']['code'] == 'delivery_version_conflict'
        dispatched = client.put(f'/api/deliveries/{did}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            after = db.scalar(select(func.count()).select_from(InventoryMovement))
        client.put(f'/api/deliveries/{did}/dispatch')
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(InventoryMovement)) == after


def test_unordered_voided_delivery_stays_blocked_without_changing_stock(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        unordered_login(client)
        created = _create_unordered_delivery(client, _unordered_payload(seed))
        did = created['id']
        assert client.put(f'/api/deliveries/{did}/dispatch').status_code == 200
        cancelled = client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        before = _stock_snapshot(factory, seed)
        payload = _unordered_payload(seed, quantity=8)
        payload.pop('delivery_date')
        payload['expected_version'] = cancelled.json()['version']
        saved = client.put(f'/api/deliveries/{did}', json=payload)
        assert saved.status_code == 409, saved.text
        assert _stock_snapshot(factory, seed) == before

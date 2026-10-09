from copy import deepcopy
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, func

from tests.test_phase7_deliveries import delivery_api_app, _create_payload, _login


def test_ordinary_create_replays_same_request_without_duplicate(delivery_api_app):
    from app.models.delivery import Delivery
    app, factory = delivery_api_app
    payload = _create_payload()
    payload['idempotency_key'] = 'chain-delivery-create-01'
    with TestClient(app) as client:
        _login(client, 'admin')
        first = client.post('/api/deliveries', json=payload)
        assert first.status_code == 201, first.text
        replay = client.post('/api/deliveries', json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()['id'] == first.json()['id']
        changed = deepcopy(payload)
        changed['items'][0]['delivered_quantity'] += 1
        conflict = client.post('/api/deliveries', json=changed)
        assert conflict.status_code == 409, conflict.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Delivery)) == 1


def test_pending_edit_accepts_ui_idempotency_key_and_replay(delivery_api_app):
    app, factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, 'admin')
        created = client.post('/api/deliveries', json=_create_payload()).json()
        payload = _create_payload()
        payload.pop('customer_id')
        payload.update(expected_version=created['version'], idempotency_key='chain-delivery-edit-01')
        payload['items'][0]['delivered_quantity'] = 25
        first = client.put(f"/api/deliveries/{created['id']}", json=payload)
        assert first.status_code == 200, first.text
        replay = client.put(f"/api/deliveries/{created['id']}", json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        payload['idempotency_key'] = 'chain-delivery-edit-stale'
        stale = client.put(f"/api/deliveries/{created['id']}", json=payload)
        assert stale.status_code == 409


def test_saved_delivery_retry_next_day_returns_original(delivery_api_app, monkeypatch):
    from app.api import deliveries
    app, factory = delivery_api_app
    today = deliveries.beijing_today()
    payload = _create_payload()
    payload.update(delivery_date=today.isoformat(), idempotency_key='chain-delivery-midnight')
    with TestClient(app) as client:
        _login(client, 'admin')
        first = client.post('/api/deliveries', json=payload)
        assert first.status_code == 201, first.text
        monkeypatch.setattr(deliveries, 'beijing_today', lambda: today + timedelta(days=1))
        replay = client.post('/api/deliveries', json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json() == first.json()


def test_normalized_source_never_silently_drops_delivery_lines(delivery_api_app):
    app, factory = delivery_api_app
    payload = _create_payload()
    for row in payload['items']:
        row['source_type'] = ' order '
    with TestClient(app) as client:
        _login(client, 'admin')
        response = client.post('/api/deliveries', json=payload)
        assert response.status_code == 201, response.text
        assert response.json()['total_quantity'] == sum(row['delivered_quantity'] for row in payload['items'])
        assert len(response.json()['items']) == len(payload['items'])


def test_blank_revision_key_is_validation_error_not_server_error(delivery_api_app):
    app, factory = delivery_api_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, 'admin')
        created = client.post('/api/deliveries', json=_create_payload()).json()
        payload = _create_payload()
        payload.pop('customer_id')
        payload.update(expected_version=created['version'], idempotency_key='        ')
        response = client.put(f"/api/deliveries/{created['id']}/revision", json=payload)
        assert response.status_code == 422, response.text


def test_pending_edit_claim_compares_version_in_same_write(delivery_api_app):
    from app.models.delivery import Delivery
    app, factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, 'admin')
        created = client.post('/api/deliveries', json=_create_payload()).json()
        payload = _create_payload()
        payload.pop('customer_id')
        payload['expected_version'] = created['version']
        engine = factory.kw['bind']
        triggered = []
        def concurrent_save(conn, cursor, statement, parameters, context, executemany):
            if not triggered and statement.startswith('UPDATE sales_deliveries SET'):
                triggered.append(True)
                # Change persisted state after the request read, before its claim.
                cursor.execute('UPDATE sales_deliveries SET version = version + 1, vehicle_number = ? WHERE id = ?', ('CONCURRENT', created['id']))
        event.listen(engine, 'before_cursor_execute', concurrent_save)
        try:
            response = client.put(f"/api/deliveries/{created['id']}", json=payload)
        finally:
            event.remove(engine, 'before_cursor_execute', concurrent_save)
        assert triggered
        assert response.status_code == 409, response.text


def test_ordinary_delivery_ui_keeps_same_key_for_same_retry(tmp_path):
    from tests.test_p1_09c_102_delivery_save_guard import _method_body, _run_node
    method = _method_body('saveModal')
    start = method.index('const requestSignature = JSON.stringify(payload);')
    end = method.index('const savedDelivery = await this.saveCurrentDeliveryDraft', start)
    script = f"""
const assert = require('node:assert/strict');
let key = 0;
globalThis.createIdempotencyKey = () => 'delivery-test-' + (++key);
const vm = {{deliveryForm:{{}}}};
const apply = new Function('payload', {json.dumps(method[start:end])}).bind(vm);
const first = {{customer_id:1,items:[{{order_item_id:5,delivered_quantity:20}}]}};
const retry = structuredClone(first);
apply(first); apply(retry);
assert.equal(first.idempotency_key,retry.idempotency_key);
const changed = {{customer_id:1,items:[{{order_item_id:5,delivered_quantity:21}}]}};
apply(changed);
assert.notEqual(first.idempotency_key,changed.idempotency_key);
"""
    _run_node(script, tmp_path, 'ordinary-delivery-idempotency.js')

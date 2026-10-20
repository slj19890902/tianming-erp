from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.order import Order
from app.models.order_import_source import OrderImportSource, OrderImportSourceLine
from tests.test_semi_finished_order_reservation import b1_app, login, fictional_document_evidence
from tests.test_order_import_save_recovery_api import original


@pytest.mark.parametrize('kind', ['pdf', 'xlsx'])
def test_cancelled_file_reimports_without_reviving_old_order_and_replays_new(b1_app, kind):
    app, factory = b1_app
    body = original(app, factory, kind=kind)
    with TestClient(app) as client:
        login(client)
        first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        old_id = first.json()['id']
        with factory() as db:
            db.get(Order, old_id).status = 'cancelled'
            db.commit()
        fresh = deepcopy(body)
        fresh['idempotency_key'] = 'fresh-import-after-cancel'
        fresh['items'][0]['quantity'] = 12
        second = client.post('/api/orders', json=fresh)
        assert second.status_code == 201, second.text
        assert second.json()['id'] != old_id
        assert second.json()['status'] != 'cancelled'
        retry = deepcopy(fresh)
        retry['idempotency_key'] = 'same-file-new-active-order'
        replay = client.post('/api/orders', json=retry)
        assert replay.status_code == 201, replay.text
        assert replay.json()['id'] == second.json()['id']
        assert replay.json()['source_replay'] is True
        changed = deepcopy(retry)
        changed['idempotency_key'] = 'active-file-changed-quantity'
        changed['items'][0]['quantity'] = 13
        assert client.post('/api/orders', json=changed).status_code == 409
        # A retry of the original request must never become a new import.
        old_replay = client.post('/api/orders', json=body)
        assert old_replay.status_code == 201 and old_replay.json()['id'] == old_id
        located = client.post('/api/orders/create-attempts/unknown-import-key/resolve',
            json={'expected_actor_id': 2, 'source_preview_token': fresh['pdf_import_confirmation']['preview_safety_token']})
        assert located.status_code == 200, located.text
        assert located.json()['order']['id'] == second.json()['id']
    with factory() as db:
        assert db.get(Order, old_id).status == 'cancelled'
        assert db.get(Order, old_id).items[0].quantity == 10
        sources = list(db.scalars(select(OrderImportSource).order_by(OrderImportSource.id)))
        assert len(sources) == 2
        assert sources[0].source_key == 'a' * 64
        assert sources[0].order_id == old_id
        assert json.loads(sources[1].confirmation_summary_json)['supersedes_source_id'] == sources[0].id
        assert db.scalar(select(func.count()).select_from(OrderImportSourceLine)) == 2


def test_actual_guarded_delete_preserves_source_and_allows_fresh_import(b1_app):
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        login(client, 'admin')
        deleted = client.delete(f"/api/orders/{first.json()['id']}?confirm=true")
        assert deleted.status_code == 204, deleted.text
        login(client)
        fresh = deepcopy(body)
        fresh['idempotency_key'] = 'reimport-after-guarded-delete'
        created = client.post('/api/orders', json=fresh)
        assert created.status_code == 201, created.text
        # SQLite can reuse a deleted row ID; old request keys must never point
        # at a different order just because its numeric ID happens to match.
        old_key_replay = client.post('/api/orders', json=body)
        assert old_key_replay.status_code == 409, old_key_replay.text
        old_resolve = client.post(f"/api/orders/create-attempts/{body['idempotency_key']}/resolve",
            json={'expected_actor_id': 2, 'original_request': body})
        assert old_resolve.status_code == 409, old_resolve.text
    with factory() as db:
        sources = list(db.scalars(select(OrderImportSource).order_by(OrderImportSource.id)))
        assert len(sources) == 2
        assert sources[0].order_id is None
        assert all(line.order_item_id is None for line in sources[0].lines)
        assert sources[1].order_id == created.json()['id']
        assert json.loads(sources[1].confirmation_summary_json)['supersedes_order_status'] == 'deleted'


def test_concurrent_reimport_after_cancel_creates_only_one_effective_order(b1_app):
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
    with factory() as db:
        db.get(Order, first.json()['id']).status = 'cancelled'
        db.commit()
    payloads = [dict(deepcopy(body), idempotency_key=f'concurrent-reimport-{i}') for i in range(2)]
    def submit(payload):
        with TestClient(app) as client:
            login(client)
            response = client.post('/api/orders', json=payload)
            return response.status_code, response.json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, payloads))
    assert all(status == 201 for status, _ in responses), responses
    ids = {body['id'] for _, body in responses}
    assert len(ids) == 1 and first.json()['id'] not in ids
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 2
        assert db.scalar(select(func.count()).select_from(Order).where(Order.status != 'cancelled')) == 1


@pytest.mark.parametrize('ending', ['cancelled', 'deleted', 'unproven_deleted'])
def test_real_location_excel_preview_save_and_reimport(b1_app, ending):
    from tests.test_order_early_pick import workbook, preview_setup, order_item
    app, factory = b1_app
    preview_setup(factory)
    content = workbook()
    with TestClient(app) as client:
        login(client)
        def preview():
            response = client.post('/api/orders/import-preview-batch',
                files={'files': ('renamed.xlsx', content)},
                data={'customer_id': 1, 'delivery_date': '2026-10-20'})
            assert response.status_code == 200, response.text
            return response.json()['drafts'][0]
        draft = preview()
        body = dict(customer_id=1, customer_po=None, delivery_date=draft['delivery_date'],
            idempotency_key='real-xlsx-before-void', import_draft=True,
            import_integrity_status='passed',
            pdf_import_confirmation={'preview_safety_token': draft['preview_safety_token'], 'confirmed': True},
            items=[{**order_item(1, row['quantity'], line=f'S{i}'), 'unit_price': row['unit_price']}
                   for i, row in enumerate(draft['items'])])
        first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        assert preview()['duplicate_status'] == 'duplicate_skipped'
        if ending == 'cancelled':
            with factory() as db:
                db.get(Order, first.json()['id']).status = 'cancelled'
                db.commit()
        elif ending == 'deleted':
            login(client, 'admin')
            assert client.delete(f"/api/orders/{first.json()['id']}?confirm=true").status_code == 204
            login(client)
        else:
            with factory() as db:
                source = db.scalar(select(OrderImportSource))
                source.order_id = None
                for line in source.lines:
                    line.order_item_id = None
                db.commit()
        new_draft = preview()
        if ending == 'unproven_deleted':
            assert new_draft['recognition_status'] == 'failed'
            assert '删除审计' in ' '.join(new_draft['warnings'])
            return
        assert new_draft.get('duplicate_status') != 'duplicate_skipped', new_draft
        assert len(new_draft['items']) == 2
        body['idempotency_key'] = 'real-xlsx-reimport-after-void'
        body['pdf_import_confirmation']['preview_safety_token'] = new_draft['preview_safety_token']
        second = client.post('/api/orders', json=body)
        assert second.status_code == 201, second.text
        assert second.json()['order_number'] != first.json()['order_number']
        final_preview = preview()
        assert final_preview['duplicate_status'] == 'duplicate_skipped'
        assert final_preview['duplicate_order_id'] == second.json()['id']


def test_null_source_without_deletion_audit_is_not_assumed_deleted(b1_app):
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        with factory() as db:
            source = db.scalar(select(OrderImportSource))
            source.order_id = None
            for line in source.lines:
                line.order_item_id = None
            db.commit()
        body['idempotency_key'] = 'orphan-is-not-deletion-proof'
        rejected = client.post('/api/orders', json=body)
        assert rejected.status_code == 409
        assert '删除审计' in rejected.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 1
        assert db.scalar(select(func.count()).select_from(Order)) == 1


@pytest.mark.parametrize('status', ['pending_production', 'closed', 'dead'])
def test_still_existing_non_cancelled_orders_keep_duplicate_protection(b1_app, status):
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        with factory() as db:
            db.get(Order, first.json()['id']).status = status
            db.commit()
        body['idempotency_key'] = 'existing-order-not-cancelled'
        body['items'][0]['quantity'] = 12
        assert client.post('/api/orders', json=body).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1


def test_reimport_failure_rolls_back_successor_and_can_retry(b1_app, monkeypatch):
    from app.services import order_import_source
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        with factory() as db:
            db.get(Order, first.json()['id']).status = 'cancelled'
            db.commit()
        body['idempotency_key'] = 'new-import-injected-failure'
        attach = order_import_source.attach
        def fault(*args, **kwargs):
            attach(*args, **kwargs)
            raise RuntimeError('injected after successor source flush')
        with monkeypatch.context() as patch:
            patch.setattr(order_import_source, 'attach', fault)
            failed = client.post('/api/orders', json=body)
        assert failed.status_code == 500, failed.text
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 1
            assert db.scalar(select(func.count()).select_from(Order)) == 1
        body['idempotency_key'] = 'new-import-after-failed-transaction'
        retry = client.post('/api/orders', json=body)
        assert retry.status_code == 201 and retry.json()['id'] != first.json()['id']

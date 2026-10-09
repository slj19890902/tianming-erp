import json
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, select, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, IntegrityError
from test_stock_replenishment_flow import stock_replenishment_app
from tests.test_stock_processing_auto import receive, row, completion
from app.api.stock_preparation import router
from app.models.stock_preparation import StockPreparationCommand as Command
from app.models.stock_preparation import StockPreparationJob as Job
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation, WarehouseLocation
from app.models.audit import OperationLog
from app.models.user import User
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.services import stock_preparation_completion_recovery as recovery
from app.services.stock_preparation_groups import encode
from app.api.stock_preparation import Action
from app.models.incoming_receipt import IncomingReceiptItem


@pytest.fixture
def recovery_app(stock_replenishment_app):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with factory() as db:
        for action in ('UPDATE', 'DELETE'):
            db.execute(text(f"CREATE TRIGGER stock_preparation_commands_no_{action.lower()} BEFORE {action} ON stock_preparation_commands BEGIN SELECT RAISE(ABORT, '备库安排流水不可修改或删除'); END"))
        db.commit()
    return app, factory


def paths(r):
    base = f'/api/production/stock-preparation/{r["receipt_item_id"]}'
    return base + '/actions', base + '/completion-result'


def query(body):
    return dict(operation_key=body['operation_key'], original_request=body, expected_actor_id=1)


def facts(factory):
    with factory() as db:
        models = (InventoryLot, Job, Command, InventoryMovement, InventoryReservation)
        values = {model.__tablename__: [tuple(row) for row in db.execute(select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns))] for model in models}
        values['business_audit'] = list(db.execute(select(OperationLog.id, OperationLog.details).where(OperationLog.event_category == 'business').order_by(OperationLog.id)))
        return values


@pytest.mark.parametrize('amount', [8, 20])
def test_fresh_receipt_is_frozen_and_resolves_with_append_only_commands(recovery_app, amount):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r, input=amount), expected_actor_id=1)
        write, resolve = paths(r)
        response = client.post(write, json=body)
        assert response.status_code == 200, response.text
        proof = response.json()['completion_receipt']
        assert proof['actual_input_quantity'] == proof['actual_output'] == amount
        assert proof['requested_job_id'] == proof['original_job_id'] == body['job_id']
        assert proof['remaining_input_quantity'] == 20 - amount
        assert proof['actor_id'] == response.json()['current_actor_id'] == 1
        assert response.headers['cache-control'] == 'no-store'
        after = facts(factory)
        saved = client.post(resolve, json=query(body))
        assert saved.status_code == 200, saved.text
        assert saved.json()['status'] == 'completed'
        assert saved.json()['completion_receipt'] == proof
        replay = client.post(write, json=body)
        assert replay.json() == response.json()
        assert facts(factory) == after
    with factory() as db:
        record = db.get(Command, body['operation_key'])
        assert 'expected_actor_id' not in json.loads(record.request_json)
        assert json.loads(record.result_json)['completion_receipt'] == proof


def test_commit_ack_loss_resolves_without_any_business_commit_or_extra_stock(recovery_app, monkeypatch):
    app, factory = recovery_app
    with TestClient(app, raise_server_exceptions=False) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        real = Session.commit
        def lose(db):
            real(db)
            raise OperationalError('synthetic committed ack loss', {}, Exception('lost'))
        with monkeypatch.context() as patch:
            patch.setattr(Session, 'commit', lose)
            response = client.post(write, json=body)
        assert response.status_code == 500
        assert response.headers['x-production-completion-preserve'] == '1'
        assert response.headers['cache-control'] == 'no-store'
        after = facts(factory)
        def no_commit(db):
            raise AssertionError('readonly query committed')
        with monkeypatch.context() as patch:
            patch.setattr(Session, 'commit', no_commit)
            saved = client.post(resolve, json=query(body))
        assert saved.status_code == 200 and saved.json()['status'] == 'completed', saved.text
        assert facts(factory) == after
        replay = client.post(write, json=body)
        assert replay.status_code == 200 and replay.json()['completion_receipt'] == saved.json()['completion_receipt']
        assert facts(factory) == after


@pytest.mark.parametrize('fault', ['proof', 'audit'])
def test_precommit_failure_rolls_back_jobs_inventory_audit_and_all_commands(recovery_app, monkeypatch, fault):
    from app.services import stock_preparation
    app, factory = recovery_app
    with TestClient(app, raise_server_exceptions=False) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, _ = paths(r); before = facts(factory)
        def failure(*args, **kwargs):
            raise RuntimeError('synthetic proof/audit failure')
        with monkeypatch.context() as patch:
            patch.setattr(recovery if fault == 'proof' else stock_preparation,
                'build_receipt' if fault == 'proof' else 'append_audit_event', failure)
            rejected = client.post(write, json=body)
        assert rejected.status_code == 500
        assert 'x-production-completion-rejected' not in rejected.headers
        assert facts(factory) == before
        assert client.post(write, json=body).status_code == 200


@pytest.mark.parametrize('target', ['write', 'resolve'])
@pytest.mark.parametrize('actor', [True, '1', 2])
def test_actor_mismatch_or_invalid_actor_never_writes(recovery_app, target, actor):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=actor); write, resolve = paths(r); before = facts(factory)
        request = body if target == 'write' else dict(query(body), expected_actor_id=actor)
        response = client.post(write if target == 'write' else resolve, json=request)
        assert response.status_code == (409 if actor == 2 and type(actor) is int else 422)
        if response.status_code == 409:
            assert response.headers['x-production-completion-actor-mismatch'] == '1'
            assert 'x-production-completion-rejected' not in response.headers
        assert facts(factory) == before


def test_not_recorded_is_readonly_and_wrong_key_body_preserved(recovery_app):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); _, resolve = paths(r); before = facts(factory)
        response = client.post(resolve, json=query(body))
        assert response.status_code == 200 and response.json() == dict(status='not_recorded', operation_key=body['operation_key'], current_actor_id=1, completion_receipt=None, result=None, trace_url=None)
        assert response.headers['cache-control'] == 'no-store'
        bad = dict(query(body), operation_key='different-original-key')
        assert client.post(resolve, json=bad).status_code == 409
        assert facts(factory) == before


def test_changed_body_or_nested_actor_preserves_completed_original(recovery_app):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        assert client.post(write, json=body).status_code == 200
        after = facts(factory)
        changed = dict(body, actual_output=body['actual_output'] + 1)
        for url, data in [(write, changed), (resolve, query(changed)), (resolve, query(dict(body, expected_actor_id=2)))]:
            response = client.post(url, json=data)
            assert response.status_code == 409 and response.headers['x-production-completion-preserve'] == '1'
            assert 'x-production-completion-rejected' not in response.headers
        assert facts(factory) == after


def test_old_append_only_key_without_proof_replays_exact_compact_signature(recovery_app):
    from app.services.stock_preparation_processing import process
    from app.services.bom_transactions import atomic_bom
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        with factory() as db:
            payload = recovery.normalized_payload(db, Action.model_validate(body))
            with atomic_bom(db):
                old = process(db, r['receipt_item_id'], payload, db.get(User, 1))
            db.commit()
            stored = db.get(Command, body['operation_key'])
            assert stored.request_json == encode(dict(payload, receipt_id=r['receipt_item_id']))
            assert 'completion_receipt' not in json.loads(stored.result_json)
        before = facts(factory)
        found = client.post(resolve, json=query(body))
        assert found.status_code == 200 and found.json()['status'] == 'legacy_trace'
        assert found.json()['completion_receipt'] is None and found.json()['result'] == old
        replay = client.post(write, json=body)
        assert replay.status_code == 200 and replay.json()['proof_status'] == 'legacy_trace'
        assert facts(factory) == before
        with factory() as db:
            with pytest.raises(IntegrityError):
                db.execute(text('UPDATE stock_preparation_commands SET result_json = result_json WHERE operation_key=:key'), {'key': body['operation_key']})
            db.rollback()
            with pytest.raises(IntegrityError):
                db.execute(text('DELETE FROM stock_preparation_commands WHERE operation_key=:key'), {'key': body['operation_key']})
            db.rollback()


def test_current_posting_location_and_stock_changes_do_not_rewrite_frozen_receipt(recovery_app, monkeypatch):
    from app.services import stock_preparation
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        proof = client.post(write, json=body).json()['completion_receipt']
        with factory() as db:
            location = db.get(WarehouseLocation, proof['location_id']); location.location_name = '后来改名'; location.is_active = False
            source = db.get(InventoryLot, proof['source_lot_id']); source.version += 1
            output = db.get(InventoryLot, proof['output_lot_id']); output.version += 1
            db.commit()
        before = facts(factory)
        def disallow(*args, **kwargs):
            raise AssertionError('readonly queried write eligibility')
        monkeypatch.setattr(stock_preparation, 'source', disallow)
        response = client.post(resolve, json=query(body))
        assert response.status_code == 200, response.text
        assert response.json()['completion_receipt'] == proof
        assert facts(factory) == before


@pytest.mark.parametrize('corruption', ['json', 'proof', 'null'])
def test_corrupt_command_inserted_without_updating_history_is_not_completed(recovery_app, corruption):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); _, resolve = paths(r)
        with factory() as db:
            payload = recovery.normalized_payload(db, Action.model_validate(body))
            broken = '{' if corruption == 'json' else encode(dict(action='complete', completion_receipt=None if corruption == 'null' else {}))
            db.add(Command(operation_key=body['operation_key'], receipt_item_id=r['receipt_item_id'], actor_id=1,
                request_json=encode(dict(payload, receipt_id=r['receipt_item_id'])), result_json=broken)); db.commit()
        before = facts(factory)
        response = client.post(resolve, json=query(body))
        assert response.status_code == 409 and response.headers['x-production-completion-preserve'] == '1'
        assert facts(factory) == before


def test_stale_first_request_has_rejection_but_existing_key_conflict_keeps_preserve(recovery_app):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, _ = paths(r)
        before = facts(factory)
        stale = dict(body, lot_version=body['lot_version'] + 100)
        response = client.post(write, json=stale)
        assert response.status_code == 409 and response.headers['x-production-completion-rejected'] == '1'
        assert facts(factory) == before
        assert client.post(write, json=body).status_code == 200
        conflict = client.post(write, json=stale)
        assert conflict.status_code == 409 and conflict.headers['x-production-completion-preserve'] == '1'
        assert 'x-production-completion-rejected' not in conflict.headers


def test_role_revocation_and_anonymous_resolve_preserve_security_denial(recovery_app):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        assert client.post(write, json=body).status_code == 200
        before = facts(factory)
        with factory() as db:
            db.get(User, 1).role = 'sales'; db.commit()
        response = client.post(resolve, json=query(body))
        assert response.status_code == 403 and response.headers['cache-control'] == 'no-store'
        assert facts(factory) == before
    with TestClient(app) as anonymous:
        response = anonymous.post(resolve, json=query(body))
        assert response.status_code == 401 and response.headers['cache-control'] == 'no-store'


def test_inflight_not_recorded_cannot_cancel_original_transaction(recovery_app, monkeypatch):
    app, factory = recovery_app
    entered, release = Event(), Event()
    real = recovery.build_receipt
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'probe release timed out'
        return real(*args, **kwargs)
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        monkeypatch.setattr(recovery, 'build_receipt', paused)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, write, json=body)
            try:
                assert entered.wait(10)
                result = client.post(resolve, json=query(body))
                assert result.status_code == 200 and result.json()['status'] == 'not_recorded', result.text
            finally:
                release.set()
            completed = pending.result(timeout=10)
        assert completed.status_code == 200, completed.text
        after = facts(factory)
        found = client.post(resolve, json=query(body))
        assert found.json()['status'] == 'completed'
        assert found.json()['completion_receipt'] == completed.json()['completion_receipt']
        assert facts(factory) == after


def test_frozen_yield_units_and_too_small_remainder_keep_original_algorithm(recovery_app):
    from test_stock_replenishment_flow import _customer_replenishment_payload
    from app.models.product import Product
    app, factory = recovery_app
    with TestClient(app) as client:
        payload = _customer_replenishment_payload(30)
        payload['items'][0]['pieces_per_box'] = 3
        receive(client, payload); r = row(client)
        body = dict(completion(r, input=19), expected_actor_id=1); write, resolve = paths(r)
        with factory() as db:
            product = db.get(Product, 1); product.product_name = 'changed after frozen'; product.version += 1; db.commit()
        result = client.post(write, json=body)
        assert result.status_code == 200, result.text
        proof = result.json()['completion_receipt']
        assert (proof['actual_input_quantity'], proof['actual_output'], proof['remaining_input_quantity']) == (19, 6, 1)
        assert proof['continuation_job_id'] is None
        with factory() as db:
            completed = db.get(Job, proof['completed_job_id'])
            assert json.loads(completed.product_snapshot)['name'] != 'changed after frozen'
            assert proof['output_stock_quantity'] == db.get(InventoryMovement, proof['output_movement_id']).quantity
            assert proof['output_stock_unit'] == db.get(InventoryMovement, proof['output_movement_id']).unit
        assert client.post(resolve, json=query(body)).json()['completion_receipt'] == proof


def test_nullable_original_customer_is_not_a_new_completion_gate(recovery_app):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        with factory() as db:
            receipt = db.get(IncomingReceiptItem, r['receipt_item_id'])
            db.get(StockReplenishmentOrderItem, receipt.stock_replenishment_item_id).customer_id = None
            db.commit()
        result = client.post(write, json=body)
        assert result.status_code == 200, result.text
        assert result.json()['completion_receipt']['customer_id'] is None
        assert client.post(resolve, json=query(body)).json()['status'] == 'completed'


def test_omitted_output_kind_resolves_from_command_not_current_job_snapshot(recovery_app):
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); body.pop('output_kind'); write, resolve = paths(r)
        response = client.post(write, json=body)
        assert response.status_code == 200 and response.json()['completion_receipt']['request']['output_kind'] == 'semi'
        with factory() as db:
            original = db.get(Job, body['job_id']); snapshot = json.loads(original.product_snapshot)
            snapshot['auto_planned'] = False; original.product_snapshot = encode(snapshot); db.commit()
        found = client.post(resolve, json=query(body))
        assert found.status_code == 200, found.text
        assert found.json()['completion_receipt'] == response.json()['completion_receipt']


def test_frozen_and_current_customer_gates_are_both_called_before_receipt_return(recovery_app, monkeypatch):
    from app.api import stock_preparation as api
    from fastapi import HTTPException
    app, factory = recovery_app
    with TestClient(app) as client:
        receive(client); r = row(client); body = dict(completion(r), expected_actor_id=1); write, resolve = paths(r)
        response = client.post(write, json=body); frozen = response.json()['completion_receipt']['customer_id']
        called = []
        real = api.require_customer_access
        def check(customer, *args):
            called.append(customer)
            return real(customer, *args)
        monkeypatch.setattr(api, 'require_customer_access', check)
        assert client.post(resolve, json=query(body)).status_code == 200
        assert called == [frozen, frozen]
        def denied(*args):
            raise HTTPException(403, 'synthetic current scope revoked')
        before = facts(factory); monkeypatch.setattr(api, 'require_customer_access', denied)
        result = client.post(resolve, json=query(body))
        assert result.status_code == 403 and result.headers['x-production-completion-preserve'] == '1'
        assert facts(factory) == before

from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from tests.test_semi_finished_order_reservation import (
    b1_app, fictional_document_evidence, login, add_finished_lot, finished_plan,
)
from tests.test_t02_order_import_source_identity import _ready_product, _payload
from app.models.order import Order, OrderItem
from app.models.audit import OperationLog
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryReservation


def original(app, factory, key="save-recovery-original", source="a" * 64, kind="pdf"):
    _ready_product(factory)
    body = _payload(app, source, key, customer_po=None)
    if kind == "xlsx":
        from app.api.orders import _encode_pdf_preview_safety_token
        with factory() as db:
            user = db.get(User, 2)
        body["pdf_import_confirmation"]["preview_safety_token"] = _encode_pdf_preview_safety_token(
            {"source_name": "synthetic.xlsx", "source_format": "xlsx", "file_hash": source,
                "items": [{"quantity": 10, "unit_price": "1", "product_code": "B1-P1"}],
                "recognition_status": "needs_confirmation", "customer_route": {"status": "needs_confirmation"},
                "customer_match_status": "matched", "integrity_check": {"integrity_status": "passed"},
                "matched_customer_id": 1}, user)
    return body


def resolve(client, body, actor=2, **extra):
    return client.post("/api/orders/create-attempts/" + body["idempotency_key"] + "/resolve",
        json={"expected_actor_id": actor, "original_request": body, **extra})


def test_first_receipt_resolve_and_exact_replay_preserve_quantity(b1_app):
    app, factory = b1_app
    body = original(app, factory)
    lot_id, version = add_finished_lot(factory, product_id=1, quantity=20, key="save-recovery-stock")
    body["items"][0]["reservation_plan"] = {"finished": [finished_plan(lot_id, version, 10)], "semi": []}
    body["expected_actor_id"] = 2
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/orders", json=body)
        assert first.status_code == 201, first.text
        proof = first.json()["save_receipt"]
        assert proof["proof_status"] == "complete" and proof["request_match"] is True
        assert proof["actor_id"] == 2 and first.json()["current_actor_id"] == 2
        assert proof["request_key"] == body["idempotency_key"]
        assert "preview_safety_token" not in json.dumps(proof)
        found = resolve(client, body)
        assert found.status_code == 200, found.text
        assert found.json()["save_receipt"] == proof
        replay = client.post("/api/orders", json=body)
        assert replay.status_code == 201 and replay.json()["save_receipt"] == proof
        old_body = deepcopy(body); old_body.pop("expected_actor_id")
        old_replay = client.post("/api/orders", json=old_body)
        assert old_replay.status_code == 201 and old_replay.json()["save_receipt"] == proof
        assert first.headers["cache-control"] == found.headers["cache-control"] == "no-store"
        changed = deepcopy(body); changed["items"][0]["quantity"] = 11
        conflict = resolve(client, changed)
        assert conflict.status_code == 409 and conflict.headers["x-order-save-preserve"] == "1"
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (10, 10, version + 1)
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(InventoryReservation)) == 1


@pytest.mark.parametrize("actor", [True, "2", 0, -1])
def test_actor_is_strict_on_write_and_resolve(b1_app, actor):
    app, factory = b1_app
    body = original(app, factory); body["expected_actor_id"] = actor
    with TestClient(app) as client:
        login(client)
        assert client.post("/api/orders", json=body).status_code == 422
        result = resolve(client, body, actor=actor)
        assert result.status_code == 422 and result.headers["cache-control"] == "no-store"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_owner_path_and_business_conflicts_preserve_request(b1_app):
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        other = deepcopy(body); other["expected_actor_id"] = 1
        rejected = client.post("/api/orders", json=other)
        assert rejected.status_code == 409
        assert rejected.headers["x-order-save-actor-mismatch"] == "1"
        assert "x-order-save-rejected" not in rejected.headers
        assert resolve(client, body, actor=1).headers["x-order-save-actor-mismatch"] == "1"
        assert resolve(client, other).headers["x-order-save-actor-mismatch"] == "1"
        wrong_key = deepcopy(body); wrong_key["idempotency_key"] = "another-original-key"
        result = client.post("/api/orders/create-attempts/" + body["idempotency_key"] + "/resolve",
            json={"expected_actor_id": 2, "original_request": wrong_key})
        assert result.status_code == 409 and "x-order-save-rejected" not in result.headers


def test_first_stock_cas_rejection_is_marked_but_commit_ack_loss_is_preserved(b1_app, monkeypatch):
    app, factory = b1_app
    body = original(app, factory)
    lot_id, version = add_finished_lot(factory, product_id=1, quantity=20, key="recovery-cas-stock")
    body["items"][0]["reservation_plan"] = {"finished": [finished_plan(lot_id, version, 10)], "semi": []}
    with factory() as db:
        db.get(InventoryLot, lot_id).version += 1; db.commit()
    with TestClient(app) as client:
        login(client)
        stale = client.post("/api/orders", json=body)
        assert stale.status_code == 409, stale.text
        assert stale.headers["x-order-save-rejected"] == "1"
        assert "x-order-save-preserve" not in stale.headers
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order)) == 0
            lot = db.get(InventoryLot, lot_id)
            assert (lot.quantity_available, lot.quantity_reserved) == (20, 0)
        body["items"][0]["reservation_plan"]["finished"][0]["expected_version"] = version + 1
        real_commit = Session.commit
        def committed_then_lost(db):
            real_commit(db)
            raise OperationalError("synthetic committed ack", {}, Exception("busy"))
        with monkeypatch.context() as patch:
            patch.setattr(Session, "commit", committed_then_lost)
            lost = client.post("/api/orders", json=body)
        assert lost.status_code == 500 and lost.headers["x-order-save-preserve"] == "1"
        assert "x-order-save-rejected" not in lost.headers
        found = resolve(client, body)
        assert found.status_code == 200 and found.json()["save_receipt"]["request_match"] is True
        replay = client.post("/api/orders", json=body)
        assert replay.status_code == 201
        changed = deepcopy(body); changed["items"][0]["quantity"] = 11
        completed_conflict = client.post("/api/orders", json=changed)
        assert completed_conflict.status_code == 409
        assert completed_conflict.headers["x-order-save-preserve"] == "1"
        assert "x-order-save-rejected" not in completed_conflict.headers
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (10, 10, version + 2)
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(InventoryReservation)) == 1


@pytest.mark.parametrize("fault", ["response", "commit"])
def test_precommit_fault_rolls_back_complete_proof_and_order(b1_app, monkeypatch, fault):
    from app.api import orders as api
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        def busy(*args, **kwargs): raise OperationalError("synthetic before commit", {}, Exception("busy"))
        with monkeypatch.context() as patch:
            patch.setattr(api, "_order_response", busy) if fault == "response" else patch.setattr(Session, "commit", busy)
            rejected = client.post("/api/orders", json=body)
        assert rejected.status_code == 500 and "x-order-save-rejected" not in rejected.headers
        assert resolve(client, body).json()["status"] == "not_found"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action == "order_create_replay")) == 0


def test_not_found_during_original_transaction_does_not_cancel_later_completion(b1_app, monkeypatch):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    from app.api import orders as api
    app, factory = b1_app
    body = original(app, factory)
    arrived, release = Event(), Event()
    real = api._order_response
    def paused(*args, **kwargs):
        result = real(*args, **kwargs); arrived.set(); assert release.wait(20); return result
    with TestClient(app) as first, TestClient(app) as reader:
        login(first); login(reader)
        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
            patch.setattr(api, "_order_response", paused)
            task = pool.submit(first.post, "/api/orders", json=body)
            try:
                assert arrived.wait(10)
                observed = resolve(reader, body)
                assert observed.status_code == 200 and observed.json()["status"] == "not_found"
            finally:
                release.set()
            assert task.result(timeout=20).status_code == 201
        assert resolve(reader, body).json()["status"] == "completed"


def test_current_and_frozen_customer_scopes_and_permission_denials(b1_app):
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/orders", json=body); assert first.status_code == 201
        with factory() as db:
            user = db.get(User, 2); user.customer_access_mode = "selected"
            db.add(UserCustomerScope(user_id=2, customer_id=1))
            db.get(Order, first.json()["id"]).customer_id = 2
            db.commit()
        assert resolve(client, body).status_code == 403
        assert client.post("/api/orders", json=body).status_code == 403
        assert client.get("/api/orders/create-attempts/" + body["idempotency_key"]).status_code == 403
        with factory() as db:
            db.get(Order, first.json()["id"]).customer_id = 1
            db.add(UserPermissionOverride(user_id=2, permission_code="orders.create", is_allowed=False)); db.commit()
        denied = resolve(client, body)
        assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
        client.cookies.clear()
        assert resolve(client, body).status_code == 401


def test_no_sales_visibility_first_save_is_complete_without_price_or_token(b1_app):
    from app.models.access_control import UserPermissionOverride
    app, factory = b1_app
    body = original(app, factory)
    with factory() as db:
        db.get(User, 2).role = "workshop"
        db.add(UserPermissionOverride(user_id=2, permission_code="orders.create", is_allowed=True)); db.commit()
    with TestClient(app) as client:
        login(client)
        result = client.post("/api/orders", json=body)
        assert result.status_code == 201, result.text
        proof = result.json()["save_receipt"]
        assert proof["pricing_visible"] is False and proof["proof_status"] == "complete"
        assert proof["request_match"] is True
        assert '"unit_price":' not in json.dumps(proof) and '"requested_unit_price":' not in json.dumps(proof) and "preview_safety_token" not in json.dumps(proof)
        assert resolve(client, body).json()["save_receipt"] == proof


@pytest.mark.parametrize("kind", ["pdf", "xlsx"])
def test_source_alias_new_line_maps_original_frozen_quantity_after_edit(b1_app, kind):
    app, factory = b1_app
    body = original(app, factory, kind=kind)
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/orders", json=body)
        assert first.status_code == 201, first.text
        with factory() as db:
            db.get(OrderItem, first.json()["items"][0]["id"]).quantity = 22
            db.commit()
        alias = deepcopy(body); alias["idempotency_key"] = "save-recovery-source-alias"
        alias["items"][0]["client_line_id"] = "new-client-line"
        result = client.post("/api/orders", json=alias)
        assert result.status_code == 201, result.text
        proof = result.json()["save_receipt"]
        assert proof["source_replay"] is True and proof["proof_status"] == "complete"
        assert proof["lines"][0]["request_client_line_id"] == "new-client-line"
        assert proof["lines"][0]["original_client_line_id"] == "pdf-line-17"
        assert float(proof["order"]["items"][0]["quantity"]) == 10
        assert float(result.json()["items"][0]["quantity"]) == 22
        found = resolve(client, alias)
        assert found.status_code == 200 and found.json()["save_receipt"] == proof
        assert client.get("/api/orders/create-attempts/" + alias["idempotency_key"]).json()["status"] == "completed"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1


@pytest.mark.parametrize("kind", ["pdf", "xlsx"])
def test_legacy_key_only_and_historical_unlogged_alias_only_locate_source(b1_app, kind):
    app, factory = b1_app
    body = original(app, factory, kind=kind)
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/orders", json=body); assert first.status_code == 201
        with factory() as db:
            log = db.scalar(select(OperationLog).where(OperationLog.action == "order_create_replay"))
            record = json.loads(log.details); record.pop("save_recovery", None)
            record["response"].pop("save_receipt", None)
            log.details = json.dumps(record); db.commit()
        old = client.post("/api/orders/create-attempts/" + body["idempotency_key"] + "/resolve",
            json={"expected_actor_id": 2})
        assert old.status_code == 200, old.text
        assert old.json()["status"] == "completed" and old.json()["proof_status"] == "legacy"
        assert old.json()["save_receipt"]["request_match"] is False
        absent = client.post("/api/orders/create-attempts/historical-alias-key/resolve",
            json={"expected_actor_id": 2, "source_preview_token": body["pdf_import_confirmation"]["preview_safety_token"]})
        assert absent.status_code == 200, absent.text
        assert absent.json()["status"] == "source_located"
        assert absent.json()["save_receipt"] is None


def test_resolve_is_readonly_and_current_price_permission_is_applied(b1_app, monkeypatch):
    from app.models.access_control import UserPermissionOverride
    app, factory = b1_app
    body = original(app, factory)
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/orders", json=body); assert first.status_code == 201
        with factory() as db:
            db.add(UserPermissionOverride(user_id=2, permission_code="orders.view", is_allowed=False)); db.commit()
        with monkeypatch.context() as patch:
            original_flush = Session.flush
            def forbidden(*args, **kwargs): raise AssertionError("resolve committed business facts")
            def no_business_flush(db, *args, **kwargs):
                assert not db.new and not db.dirty and not db.deleted, "resolve flushed business facts"
                return original_flush(db, *args, **kwargs)
            patch.setattr(Session, "flush", no_business_flush); patch.setattr(Session, "commit", forbidden)
            response = resolve(client, body)
        assert response.status_code == 200, response.text
        proof = response.json()["save_receipt"]
        assert proof["pricing_visible"] is False and proof["request_match"] is True
        assert proof["proof_status"] == "complete"
        assert '"unit_price":' not in json.dumps(proof) and '"requested_unit_price":' not in json.dumps(proof)
        assert "preview_safety_token" not in json.dumps(proof)


@pytest.mark.parametrize("case", ["null-legacy", "new-null", "precision"])
def test_additive_proof_does_not_reject_valid_nonimport_creation(b1_app, case):
    app, factory = b1_app; _ready_product(factory)
    item = dict(client_line_id="compat-line", product_id=1, quantity=10, unit_price="1.123456789")
    if case == "null-legacy":
        item.update(product_id=None, product_code="B1-M1", product_name="共享规格印刷1",
            material_id=1, layer_count=3, flute_type="B")
    elif case == "new-null":
        item.update(product_id=None, is_new_product=True, product_code="NEW-RECOVERY-NULL",
            product_name="新增常用箱", specification="300×200×100mm", material_id=1,
            layer_count=3, flute_type="B")
    body = dict(customer_id=1, customer_po="", idempotency_key="compat-create-" + case,
        expected_actor_id=2, items=[item])
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/orders", json=body)
        assert response.status_code == 201, response.text
        found = resolve(client, body)
        assert found.status_code == 200 and found.json()["status"] == "completed", found.text
        proof = response.json()["save_receipt"]
        assert proof == found.json()["save_receipt"]
        assert proof["order"]["customer_po"] is None
        if case == "null-legacy":
            assert proof["proof_status"] == "legacy"
        elif case == "new-null":
            assert proof["proof_status"] == "complete"
            assert proof["lines"][0]["request_product_id"] is None
            assert proof["lines"][0]["is_new_product"] is True
            assert proof["lines"][0]["product_id"] > 0
        else:
            assert proof["proof_status"] == "complete"
            assert proof["lines"][0]["requested_unit_price"] == "1.123456789"
            assert proof["lines"][0]["unit_price"] == "1.123457"
            assert proof["lines"][0]["unit_price_scale"] == 6
            assert proof["lines"][0]["price_normalized"] is True
    if case == "precision":
        with factory() as db:
            from decimal import Decimal
            persisted = db.get(OrderItem, response.json()["items"][0]["id"])
            assert persisted.unit_price == Decimal("1.123457")


def test_import_null_new_product_keeps_existing_registration_gate(b1_app):
    app, factory = b1_app
    body = original(app, factory)
    body["items"][0].update(product_id=None, is_new_product=True, product_code="IMPORT-NEW")
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/orders", json=body)
        assert response.status_code == 400 and "唯一常用箱" in response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def precision_import(app, factory, *, actor=2):
    from app.api.orders import _encode_pdf_preview_safety_token
    body = original(app, factory)
    body['expected_actor_id'] = actor
    body['items'][0]['unit_price'] = '1.123456789'
    with factory() as db:
        user = db.get(User, actor)
    body['pdf_import_confirmation']['preview_safety_token'] = _encode_pdf_preview_safety_token(
        {'source_name': 'precision.xlsx', 'source_format': 'xlsx', 'file_hash': 'a' * 64,
         'items': [{'quantity': 10, 'unit_price': '1.123456789', 'product_code': 'B1-P1'}],
         'recognition_status': 'needs_confirmation', 'customer_route': {'status': 'needs_confirmation'},
         'customer_match_status': 'matched', 'integrity_check': {'integrity_status': 'passed'},
         'matched_customer_id': 1}, user)
    return body


@pytest.mark.parametrize('historical', [False, True])
def test_source_precision_uses_only_initial_persistent_actual_price(b1_app, historical):
    app, factory = b1_app; body = precision_import(app, factory)
    with TestClient(app) as client:
        login(client); first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        assert first.json()['save_receipt']['lines'][0]['unit_price'] == '1.123457'
        with factory() as db:
            db.get(OrderItem, first.json()['items'][0]['id']).unit_price = '8.8'
            if historical:
                log = db.scalar(select(OperationLog).where(OperationLog.action == 'order_create_replay'))
                facts = json.loads(log.details); facts.pop('save_recovery'); log.details = json.dumps(facts)
            db.commit()
        alias_body = deepcopy(body); alias_body['idempotency_key'] = 'precision-source-alias'
        alias_body['items'][0]['client_line_id'] = 'precision-alias-client'
        alias = client.post('/api/orders', json=alias_body); assert alias.status_code == 201, alias.text
        proof = alias.json()['save_receipt']
        assert proof['proof_status'] == ('legacy' if historical else 'complete')
        if not historical:
            assert proof['lines'][0]['requested_unit_price'] == '1.123456789'
            assert proof['lines'][0]['unit_price'] == '1.123457'
            assert proof['order']['items'][0]['unit_price'] == '1.123457'
        assert resolve(client, alias_body).json()['save_receipt'] == proof
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1


def test_email_existing_without_old_replay_log_adds_exact_same_key_mapping(b1_app):
    from app.models.email_intake import EmailIntakeMessage, EmailIntakeAttachment, EmailIntakeOrderLink
    app, factory = b1_app; body = precision_import(app, factory, actor=1)
    with factory() as db:
        mail = EmailIntakeMessage(mailbox_key='owned', uid_validity='1', uid=1)
        db.add(mail); db.flush()
        attachment = EmailIntakeAttachment(message_id=mail.id, part_number=1,
            filename='precision.xlsx', sha256='a' * 64, content=b'synthetic')
        db.add(attachment); db.flush(); body['email_attachment_id'] = attachment.id; db.commit()
    with TestClient(app) as client:
        login(client, 'admin'); first = client.post('/api/orders', json=body)
        assert first.status_code == 201, first.text
        with factory() as db:
            logs = list(db.scalars(select(OperationLog).where(OperationLog.action == 'order_create_replay')))
            for log in logs: db.delete(log)
            db.commit()
        replay = client.post('/api/orders', json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()['id'] == first.json()['id']
        found = resolve(client, body, actor=1)
        assert found.status_code == 200 and found.json()['status'] == 'completed', found.text
        assert found.json()['save_receipt'] == replay.json()['save_receipt']
        changed_key = deepcopy(body); changed_key['idempotency_key'] = 'email-new-key'
        rejected = client.post('/api/orders', json=changed_key)
        assert rejected.status_code == 409 and '改单' in rejected.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(EmailIntakeOrderLink)) == 1
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action == 'order_create_replay')) == 1

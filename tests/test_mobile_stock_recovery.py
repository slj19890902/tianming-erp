from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select

from app.api import mobile_stock_use
from app.models.access_control import UserPermissionOverride
from app.models.audit import OperationLog
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, WarehouseLocation
from tests.test_mobile_dimension_stock import BASE, body, stock_api
from tests.test_n035_stocktake_api import _login, stocktake_api


def snapshot(factory):
    with factory() as db:
        return (list(db.execute(select(InventoryLot.id, InventoryLot.version, InventoryLot.quantity_available,
                    InventoryLot.quantity_reserved, InventoryLot.quantity_consumed, InventoryLot.quantity_damaged,
                    InventoryLot.quantity_scrapped, InventoryLot.warehouse_location_id).order_by(InventoryLot.id))),
                list(db.execute(select(InventoryMovement.id, InventoryMovement.inventory_lot_id,
                    InventoryMovement.quantity, InventoryMovement.operator_id, InventoryMovement.remarks)
                    .order_by(InventoryMovement.id))),
                db.scalar(select(func.count(OperationLog.id)).where(OperationLog.event_category == "business")))


def resolve(client, ids, payload, *, lot=None):
    return client.post(BASE + f'/{lot or ids["lot1"]}/resolve', json=payload)


def test_old_and_new_actor_payloads_replay_same_key_and_exact_readonly_receipt(stock_api):
    app, factory, ids = stock_api
    original = body(factory, ids)
    initial = next(row for row in snapshot(factory)[0] if row[0] == ids["lot1"])
    with TestClient(app) as client:
        _login(client, "n035-admin")
        first = client.post(BASE + f'/{ids["lot1"]}/take', json=original)
        assert first.status_code == 200, first.text
        owned = {**original, "expected_actor_id": ids["admin"]}
        repeat = client.post(BASE + f'/{ids["lot1"]}/take', json=owned)
        assert repeat.json() == {**first.json(), "replayed": True}
        before = snapshot(factory)
        writes = []
        with factory() as db:
            engine = db.bind
        def capture(_connection, _cursor, statement, *_args):
            if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN"}:
                writes.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            receipt = resolve(client, ids, owned)
            missing = resolve(client, ids, {**owned, "idempotency_key": "unrecorded_original_key_1234"})
            conflict = resolve(client, ids, {**owned, "quantity": 4})
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert writes == []
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["status"] == "completed"
        assert receipt.json()["movement_id"] == first.json()["movement_id"]
        assert receipt.json()["quantity"] == 3 and receipt.json()["replayed"] is True
        assert receipt.json()["actor_id"] == ids["admin"] and receipt.json()["account"] == "n035-admin"
        assert "no-store" in receipt.headers["cache-control"]
        assert missing.json()["status"] == "not_recorded" and missing.json()["can_continue"] is False
        assert "版本" in missing.json()["continue_reason"]
        assert conflict.status_code == 409 and conflict.headers["X-Stock-Take-Preserve"] == "1"
        assert snapshot(factory) == before
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed, lot.version) == (
            initial[2] - original["quantity"], initial[3], initial[4] + original["quantity"], initial[1] + 1)
        signed = json.loads(db.scalar(select(InventoryMovement)).remarks)
        assert "expected_actor_id" not in signed


def test_actor_change_preserves_original_unknown_request_and_cross_actor_resolve_is_factual(stock_api):
    app, factory, ids = stock_api
    original = body(factory, ids, expected_actor_id=ids["workshop"])
    before = snapshot(factory)
    with TestClient(app) as client:
        _login(client, "n035-admin")
        rejection = client.post(BASE + f'/{ids["lot1"]}/take', json=original)
        assert rejection.status_code == 409
        assert rejection.headers["X-Stock-Take-Actor-Mismatch"] == "1"
        assert rejection.headers["X-Stock-Take-Preserve"] == "1"
        assert "X-Stock-Take-Rejected" not in rejection.headers
        assert snapshot(factory) == before
        legacy = {key: value for key, value in original.items() if key != "expected_actor_id"}
        missing = resolve(client, ids, legacy).json()
        assert missing["status"] == "not_recorded" and missing["can_continue"] is True
        assert missing["current_actor_id"] == ids["admin"]
        # Explicit continuation under the authenticated current account keeps
        # the exact original key and all business fields.
        first = client.post(BASE + f'/{ids["lot1"]}/take', json={**legacy, "expected_actor_id": ids["admin"]})
        assert first.status_code == 200, first.text
        _login(client, "n035-workshop")
        completed = resolve(client, ids, legacy)
        assert completed.status_code == 200, completed.text
        assert completed.json()["actor_id"] == ids["admin"]
        assert completed.json()["current_actor_id"] == ids["workshop"]
        assert completed.json()["account"] == "n035-admin"
        ambiguous = client.post(BASE + f'/{ids["lot1"]}/take', json=legacy)
        assert ambiguous.status_code == 409 and ambiguous.headers["X-Stock-Take-Preserve"] == "1"
        assert "X-Stock-Take-Rejected" not in ambiguous.headers
    with factory() as db:
        assert db.scalar(select(func.count(InventoryMovement.id))) == 1


@pytest.mark.parametrize("changes", [{"quantity": 4}, {"purpose": "cash"}, {"expected_version": 2},
                                     {"location_id": 9999}, {"address_version": 9999}])
def test_precise_original_payload_cannot_match_similar_movement(stock_api, changes):
    app, factory, ids = stock_api
    original = body(factory, ids)
    with TestClient(app) as client:
        _login(client, "n035-admin")
        assert client.post(BASE + f'/{ids["lot1"]}/take', json=original).status_code == 200
        before = snapshot(factory)
        error = resolve(client, ids, {**original, **changes})
        assert error.status_code == 409 and "X-Stock-Take-Rejected" not in error.headers
        assert error.headers["X-Stock-Take-Preserve"] == "1"
        assert "movement_id" not in error.text and snapshot(factory) == before


@pytest.mark.parametrize("corruption", ["bad_json", "actor", "lot", "quantity", "purpose", "bool_quantity"])
def test_completed_movement_and_stored_signature_must_be_consistent(stock_api, corruption):
    app, factory, ids = stock_api
    original = body(factory, ids)
    with TestClient(app) as client:
        _login(client, "n035-admin")
        assert client.post(BASE + f'/{ids["lot1"]}/take', json=original).status_code == 200
        with factory() as db:
            movement = db.scalar(select(InventoryMovement))
            if corruption == "bad_json": movement.remarks = "invalid"
            elif corruption == "actor": movement.operator_id = ids["workshop"]
            elif corruption == "lot": movement.inventory_lot_id = ids["lot2"]
            elif corruption == "quantity": movement.quantity = 4
            elif corruption == "purpose": movement.reason = "other purpose"
            else:
                signed = json.loads(movement.remarks)
                signed["quantity"] = True
                movement.remarks = json.dumps(signed)
            db.commit()
        before = snapshot(factory)
        error = resolve(client, ids, original)
        assert error.status_code == 409 and error.headers["X-Stock-Take-Preserve"] == "1"
        assert "movement_id" not in error.text and snapshot(factory) == before


@pytest.mark.parametrize("ineligible", ["version", "location", "address", "inactive_location", "frozen", "quantity", "actor", "permission", "pallet"])
def test_not_recorded_reports_current_ineligibility_without_deleting_or_writing(stock_api, ineligible):
    from app.models.warehouse_inventory import InventoryPallet, InventoryPalletItem
    app, factory, ids = stock_api
    original = body(factory, ids)
    account = "n035-admin"
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        if ineligible == "version": lot.version += 1
        elif ineligible == "location": original["location_id"] = ids["other_location"]
        elif ineligible == "address": original["address_version"] += 1
        elif ineligible == "inactive_location": lot.location.is_active = False
        elif ineligible == "frozen": lot.status = "frozen"
        elif ineligible == "quantity": original["quantity"] = lot.quantity_available + 1
        elif ineligible == "actor": original["expected_actor_id"] = ids["workshop"]
        elif ineligible == "permission":
            account = "n035-workshop"
            db.add(UserPermissionOverride(user_id=ids["workshop"], permission_code="warehouse.execute", is_allowed=False))
        else:
            pallet = InventoryPallet(pallet_code="RECOVERY-MISMATCH", location_id=ids["other_location"])
            db.add(pallet); db.flush()
            detail = lot.finished_detail
            db.add(InventoryPalletItem(pallet_id=pallet.id, inventory_lot_id=lot.id, quantity=12, unit="boxes",
                customer_id=detail.owner_customer_id, product_id=detail.product_id, item_type="finished", match_status="matched"))
        db.commit()
    before = snapshot(factory)
    with TestClient(app) as client:
        _login(client, account)
        response = resolve(client, ids, original)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "not_recorded" and result["can_continue"] is False
        assert result["continue_reason"] and result["observed_at"]
        assert snapshot(factory) == before


def test_resolve_scope_and_permission_precede_key_lookup_preserving_security_denial(stock_api):
    app, factory, ids = stock_api
    original = body(factory, ids)
    with TestClient(app) as client:
        assert resolve(client, ids, original).status_code == 401
        _login(client, "n035-admin")
        assert client.post(BASE + f'/{ids["lot1"]}/take', json=original).status_code == 200
        before = snapshot(factory)
        _login(client, "n035-restricted")
        statements = []
        with factory() as db: engine = db.bind
        def capture(_connection, _cursor, statement, *_args): statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            denied = resolve(client, ids, original)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert denied.status_code == 403 and "movement_id" not in denied.text
        assert not any("FROM inventory_movements" in statement for statement in statements)
        assert snapshot(factory) == before
        with factory() as db:
            db.add(UserPermissionOverride(user_id=ids["workshop"], permission_code="warehouse.view", is_allowed=False))
            db.commit()
            security_before = db.scalar(select(func.count(OperationLog.id)).where(
                OperationLog.event_category == "security", OperationLog.action_code == "permission.denied"))
        _login(client, "n035-workshop")
        assert resolve(client, ids, original).status_code == 403
        assert snapshot(factory) == before
        with factory() as db:
            assert db.scalar(select(func.count(OperationLog.id)).where(
                OperationLog.event_category == "security", OperationLog.action_code == "permission.denied")) == security_before + 1


def test_no_autoflush_commit_or_immediate_write_lock_in_readonly_resolver(stock_api):
    from fastapi import Response
    app, factory, ids = stock_api
    original = body(factory, ids)
    with factory() as db:
        user = db.get(User, ids["admin"])
        lot = db.get(InventoryLot, ids["lot1"])
        lot.quantity_available = 9  # Unflushed caller work must remain unflushed.
        statements = []
        def capture(_connection, _cursor, statement, *_args): statements.append(statement)
        event.listen(db.bind, "before_cursor_execute", capture)
        try:
            result = mobile_stock_use.resolve_take(ids["lot1"], mobile_stock_use.TakeRequest(**original), Response(), db, user)
            assert result["status"] == "not_recorded" and lot in db.dirty
            assert not any(statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE", "BEGIN IMMEDIATE")) for statement in statements)
        finally:
            event.remove(db.bind, "before_cursor_execute", capture)
            db.rollback()
    with factory() as db:
        assert db.get(InventoryLot, ids["lot1"]).quantity_available == 10


def test_audit_fault_rolls_back_actor_guarded_take_and_recovery_sees_no_receipt(stock_api, monkeypatch):
    app, factory, ids = stock_api
    original = body(factory, ids, expected_actor_id=ids["admin"])
    before = snapshot(factory)
    def fail(*_args, **_kwargs): raise RuntimeError("recovery-audit-fault")
    monkeypatch.setattr(mobile_stock_use, "append_audit_event", fail)
    with TestClient(app) as client:
        _login(client, "n035-admin")
        with pytest.raises(RuntimeError, match="recovery-audit-fault"):
            client.post(BASE + f'/{ids["lot1"]}/take', json=original)
        assert snapshot(factory) == before
        result = resolve(client, ids, original).json()
        assert result["status"] == "not_recorded" and result["can_continue"] is True
        assert snapshot(factory) == before

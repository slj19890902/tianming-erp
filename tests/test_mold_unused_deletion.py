from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.mold_tool import MoldMasterMutation, MoldTool, MoldToolCustomer
from tests.test_mold_tool_workflow import _login, _protected_business_state, mold_app


def _migration():
    path = Path(__file__).parents[1] / "alembic/versions/ep1010md_unused_mold_deletion.py"
    spec = importlib.util.spec_from_file_location("mold_delete_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def deletion_app(mold_app):
    app, factory = mold_app
    with factory() as db:
        db.get(Customer, 1).chinese_short_name = "测试"
        for _, statement in _migration().guard_statements():
            db.execute(text(statement))
        db.commit()
    return app, factory


def _create(client, key="unused-mold-create-001"):
    response = client.post("/api/warehouse/molds", json={
        "label_name": "误建测试模具", "customers": [{"customer_id": 1, "display_order": 1}],
        "rack_location": "1F-M-R01-L1-G01", "idempotency_key": key,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _delete(client, mold, **overrides):
    return client.request("DELETE", f"/api/warehouse/molds/{mold['id']}", json={
        "expected_version": mold["version"], "idempotency_key": "unused-mold-delete-001",
        "confirmed": True, **overrides,
    })


def test_unused_deletion_hides_identity_preserves_audit_and_replays(deletion_app):
    app, factory = deletion_app
    before = _protected_business_state(factory)
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create(client)
        mid = mold["id"]
        assert client.get(f"/api/warehouse/molds/{mid}/deletion-check").json()["eligible"]
        response = _delete(client, mold)
        assert response.status_code == 200, response.text
        assert response.json()["deleted"] is True
        assert _delete(client, mold).json()["idempotent_replay"] is True
        assert _delete(client, mold, expected_version=99).status_code == 409
        for path in ("detail", "bound-products", "label-preview", "label", "enable"):
            r = client.put(f"/api/warehouse/molds/{mid}/{path}") if path == "enable" else client.get(f"/api/warehouse/molds/{mid}/{path}")
            assert r.status_code == 404, (path, r.text)
        for flag in (False, True):
            rows = client.get("/api/warehouse/molds", params={"include_inactive": flag, "q": mold["mold_code"]}).json()
            assert rows["total"] == 0
        assert client.post(f"/api/warehouse/molds/{mid}/product-bindings", json={"items": [{"product_id": 1, "expected_version": 1}]}).status_code == 409
        assert client.post(f"/api/warehouse/molds/{mid}/repair-status", json={"target_status": "needs_repair", "expected_version": 1, "idempotency_key": "repair-deleted-001", "confirmed": True}).status_code == 404
        other = _create(client, "unused-mold-create-002")
        assert other["id"] != mid and other["mold_code"] != mold["mold_code"]
        assert _delete(client, other).status_code == 409
    with factory() as db:
        row = db.get(MoldTool, mid)
        assert row.deleted_at and row.deleted_by and not row.is_active
        assert row.version == mold["version"] + 1
        assert db.scalar(select(MoldMasterMutation).where(MoldMasterMutation.mold_tool_id == mid)).action == "create"
        assert db.scalar(select(MoldToolCustomer).where(MoldToolCustomer.mold_tool_id == mid))
        logs = db.scalars(select(OperationLog).where(OperationLog.action_code == "mold.master.delete_unused")).all()
        assert len(logs) == 1 and logs[0].entity_id == mid
        for sql in ("UPDATE mold_tools SET is_active = 1 WHERE id = :id", "DELETE FROM mold_tools WHERE id = :id"):
            with pytest.raises(IntegrityError, match="immutable|invalid mold deletion state"):
                db.execute(text(sql), {"id": mid})
            db.rollback()
        with pytest.raises(IntegrityError, match="cannot be referenced"):
            db.execute(text("INSERT INTO mold_tool_customers (mold_tool_id, customer_id, display_order, created_by) VALUES (:id, 1, NULL, 1)"), {"id": mid})
        db.rollback()
    assert _protected_business_state(factory) == before


def test_deletion_rechecks_usage_and_rolls_back_version(deletion_app):
    app, factory = deletion_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create(client)
        assert client.get(f"/api/warehouse/molds/{mold['id']}/deletion-check").json()["eligible"]
        # A usage fact arrives after preview. Deletion must recheck within its write transaction.
        with factory() as db:
            db.add(OperationLog(user_id=1, action="UNBIND_PRODUCT", resource="MOLD_TOOL",
                                entity_type="mold_tool", entity_id=mold["id"], details="{}"))
            db.commit()
        response = _delete(client, mold)
        assert response.status_code == 409 and "历史绑定" in response.text
    with factory() as db:
        row = db.get(MoldTool, mold["id"])
        assert row.version == mold["version"] and row.deleted_at is None and row.is_active


@pytest.mark.parametrize("state,reason", [
    ({"location_version": 2}, "位置确认"),
    ({"repair_version": 2}, "维修"),
    ({"last_location_confirmed_at": "2026-10-10 01:00:00"}, "位置确认"),
])
def test_used_state_cannot_be_deleted(deletion_app, state, reason):
    app, factory = deletion_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create(client)
        with factory() as db:
            column, value = next(iter(state.items()))
            db.execute(text(f"UPDATE mold_tools SET {column} = :value WHERE id = :id"), {"value": value, "id": mold["id"]})
            db.commit()
        response = _delete(client, mold)
        assert response.status_code == 409 and reason in response.text


def test_deletion_rejects_unconfirmed_stale_and_unauthorized(deletion_app, monkeypatch):
    import app.api.warehouse as warehouse
    app, factory = deletion_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create(client)
        assert _delete(client, mold, confirmed=False).status_code == 422
        assert _delete(client, mold, expected_version=99).status_code == 409
        _login(client, "sales")
        assert _delete(client, mold).status_code == 403
        assert client.get(f"/api/warehouse/molds/{mold['id']}/deletion-check").status_code == 403
        _login(client, "admin")
        monkeypatch.setattr(warehouse, "_mold_customer_scope", lambda user, db: {1})
        assert _delete(client, mold).status_code == 403


def test_audit_failure_rolls_back_deletion_and_same_key_can_retry(deletion_app, monkeypatch):
    import app.api.warehouse as warehouse
    app, factory = deletion_app
    original = warehouse.append_audit_event
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin")
        mold = _create(client)
        def fail(*args, **kwargs):
            raise RuntimeError("injected audit failure")
        monkeypatch.setattr(warehouse, "append_audit_event", fail)
        assert _delete(client, mold).status_code == 500
        with factory() as db:
            row = db.get(MoldTool, mold["id"])
            assert row.deleted_at is None and row.version == mold["version"]
        monkeypatch.setattr(warehouse, "append_audit_event", original)
        assert _delete(client, mold).status_code == 200


def test_guard_covers_all_mold_foreign_keys():
    from app.models import Base
    from app.services.mold_deletion import JSON_USAGE_REFERENCES
    actual = {(table.name, fk.parent.name) for table in Base.metadata.tables.values()
              for fk in table.foreign_keys if fk.target_fullname == "mold_tools.id"}
    assert actual <= set(_migration().REFERENCES)
    assert {(table, column) for table, column, _ in JSON_USAGE_REFERENCES} == set(_migration().JSON_REFERENCES)


def test_product_bound_then_unbound_still_blocks_deletion(deletion_app):
    app, factory = deletion_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create(client)
        response = client.post("/api/master/products", json={
            "customer_id": 1, "product_code": "MOLD-USED-001", "customer_material_code": "MOLD-USED-001", "product_name": "历史绑定测试",
            "box_category": "normal", "production_process": "模切", "mold_tool_id": mold["id"],
        })
        assert response.status_code == 201, response.text
        product = response.json()
        response = _delete(client, mold)
        assert response.status_code == 409 and "绑定" in response.text
        response = client.delete(f"/api/warehouse/molds/{mold['id']}/product-bindings/{product['id']}",
                                 params={"expected_version": product["version"]})
        assert response.status_code == 200, response.text
        response = _delete(client, mold)
        assert response.status_code == 409 and "历史" in response.text


@pytest.mark.parametrize("usage", ["print", "repair", "move", "archive"])
def test_usage_facts_prevent_deletion(deletion_app, usage):
    from app.models.mold_tool import MoldLabelPrintJob, MoldLabelPrintJobItem
    app, factory = deletion_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create(client)
        mid = mold["id"]
        if usage == "print":
            with factory() as db:
                job = MoldLabelPrintJob(idempotency_key="usage-print-001", source="single", item_count=1,
                                       printed_by=1, printed_by_username="admin")
                db.add(job); db.flush()
                db.add(MoldLabelPrintJobItem(print_job_id=job.id, mold_tool_id=mid, item_order=1,
                                            mold_code_snapshot=mold["mold_code"], rack_location_snapshot=mold["rack_location"]))
                db.commit()
        elif usage == "repair":
            r = client.post(f"/api/warehouse/molds/{mid}/repair-status", json={
                "target_status": "needs_repair", "expected_version": 1,
                "idempotency_key": "used-repair-001", "confirmed": True,
            })
            assert r.status_code == 200, r.text
        elif usage == "archive":
            r = client.post(f"/api/warehouse/molds/{mid}/archive", json={
                "expected_version": 1, "idempotency_key": "used-archive-001",
                "reason": "unbound", "physical_move_confirmed": True,
            })
            assert r.status_code == 200, r.text
        else:
            from app.models.mold_tool import MoldLocationMovement
            with factory() as db:
                db.add(MoldLocationMovement(mold_tool_id=mid, mold_code_snapshot=mold["mold_code"],
                    from_location=mold["rack_location"], to_location="1F-M-R04",
                    expected_version=1, resulting_version=2, idempotency_key="used-move-001",
                    source="manual_input"))
                db.commit()
        check = client.get(f"/api/warehouse/molds/{mid}/deletion-check").json()
        assert not check["eligible"], check
        assert _delete(client, mold).status_code == 409


def test_legacy_without_creation_evidence_is_not_assumed_unused(deletion_app):
    app, factory = deletion_app
    with factory() as db:
        row = MoldTool(mold_code="LEGACY-UNCERTAIN", mold_name="历史未核实", rack_location="R01")
        db.add(row); db.commit(); mid = row.id
    with TestClient(app) as client:
        _login(client, "admin")
        response = _delete(client, {"id": mid, "version": 1})
        assert response.status_code == 409 and "缺少新建凭证" in response.text


def test_json_position_receipt_blocks_deletion_and_cannot_be_added_after_delete(deletion_app):
    from app.models.fixed_shelf import ShelfMutation
    app, factory = deletion_app
    with TestClient(app) as client:
        _login(client, "admin")
        used = _create(client)
        with factory() as db:
            db.add(ShelfMutation(idempotency_key="position-no-op", request_hash="a" * 64,
                                 result_json=json.dumps({"mold_id": used["id"], "no_change": True})))
            db.commit()
        response = _delete(client, used)
        assert response.status_code == 409 and "位置操作" in response.text
        unused = _create(client, "unused-mold-create-002")
        assert _delete(client, unused).status_code == 200
    with factory() as db:
        db.add(ShelfMutation(idempotency_key="late-position", request_hash="b" * 64,
                             result_json=json.dumps({"mold_id": unused["id"]})))
        with pytest.raises(IntegrityError, match="cannot be referenced"):
            db.commit()


def test_same_command_committed_while_waiting_for_writer_returns_receipt(deletion_app, monkeypatch):
    from app.services.mold_deletion import delete_unused_mold
    app, factory = deletion_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create(client)
    payload = dict(mold_id=mold["id"], expected_version=mold["version"],
                   idempotency_key="concurrent-delete-001", actor_id=1)
    with factory() as waiting:
        original_execute = waiting.execute
        completed = False
        def execute(statement, *args, **kwargs):
            nonlocal completed
            if not completed and str(statement).startswith("UPDATE mold_tools SET version="):
                completed = True
                with factory() as winner:
                    _, replayed = delete_unused_mold(winner, **payload)
                    assert not replayed
                    winner.commit()
            return original_execute(statement, *args, **kwargs)
        monkeypatch.setattr(waiting, "execute", execute)
        row, replayed = delete_unused_mold(waiting, **payload)
        assert completed and replayed and row.version == mold["version"] + 1
        waiting.commit()

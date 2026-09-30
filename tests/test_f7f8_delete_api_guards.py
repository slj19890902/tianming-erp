from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import warehouse as warehouse_api
from app.api.deps import get_db
from app.models.audit import OperationLog
from app.models.user import User
from app.models.warehouse_inventory import WarehouseLocation
from tests.test_f7f8_published_rack_delete import _setup


def _app(factory, admin=None, *, forbidden=False):
    app = FastAPI()
    app.include_router(warehouse_api.router, prefix="/api/warehouse")
    def db_dep():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = db_dep
    if forbidden:
        def deny(): raise HTTPException(status_code=403, detail="forbidden")
        app.dependency_overrides[warehouse_api.admin_only] = deny
    elif admin is not None:
        app.dependency_overrides[warehouse_api.admin_only] = lambda: admin
    return app


def _params(pub, draft, *, key="api-rack-delete-01", version=3):
    return {"expected_published_revision": pub, "expected_revision": draft,
            "expected_version": version, "operation_key": key}


def test_delete_api_authentication_and_role_guards(tmp_path, monkeypatch):
    factory,pub,draft,rack_id,_runtime,_draft=_setup(tmp_path,monkeypatch)
    path=f"/api/warehouse/twin-layout/floors/3F/racks/{rack_id}"
    assert TestClient(_app(factory)).delete(path, params=_params(pub,draft)).status_code == 401
    assert TestClient(_app(factory, forbidden=True)).delete(path, params=_params(pub,draft)).status_code == 403


def test_delete_api_admin_replay_and_same_key_conflict(tmp_path, monkeypatch):
    factory,pub,draft,rack_id,_runtime,_draft=_setup(tmp_path,monkeypatch)
    with factory() as db: admin=db.scalar(select(User))
    client=TestClient(_app(factory,admin)); path=f"/api/warehouse/twin-layout/floors/3F/racks/{rack_id}"
    first=client.delete(path,params=_params(pub,draft)); assert first.status_code == 200 and first.json()["applied"]
    replay=client.delete(path,params=_params(pub,draft)); assert replay.status_code == 200 and not replay.json()["applied"]
    conflict=client.delete(path,params=_params(pub,draft,version=2)); assert conflict.status_code == 409
    with factory() as db:
        assert len(db.scalars(select(OperationLog).where(OperationLog.action=="TWIN_RACK_DELETE_PUBLISHED")).all()) == 1


def test_delete_api_stale_and_commit_failure_leave_files_and_slots_intact(tmp_path, monkeypatch):
    factory,pub,draft,rack_id,runtime,draft_path=_setup(tmp_path,monkeypatch)
    before_runtime, before_draft=runtime.read_bytes(),draft_path.read_bytes()
    with factory() as db: admin=db.scalar(select(User))
    client=TestClient(_app(factory,admin)); path=f"/api/warehouse/twin-layout/floors/3F/racks/{rack_id}"
    assert client.delete(path,params=_params(pub,"stale")).status_code == 409
    original=warehouse_api._twin_layout_asset_log
    monkeypatch.setattr(warehouse_api,"_twin_layout_asset_log",lambda *a,**k: (_ for _ in ()).throw(RuntimeError("audit fail")))
    with pytest.raises(RuntimeError): client.delete(path,params=_params(pub,draft))
    assert runtime.read_bytes()==before_runtime and draft_path.read_bytes()==before_draft
    with factory() as db: assert all(x.is_active for x in db.scalars(select(WarehouseLocation)).all())
    monkeypatch.setattr(warehouse_api,"_twin_layout_asset_log",original)


def test_delete_api_rejects_receipt_without_committed_audit(tmp_path, monkeypatch):
    factory,pub,draft,rack_id,_runtime,_draft=_setup(tmp_path,monkeypatch)
    with factory() as db: admin=db.scalar(select(User))
    client=TestClient(_app(factory,admin)); path=f"/api/warehouse/twin-layout/floors/3F/racks/{rack_id}"
    assert client.delete(path,params=_params(pub,draft)).status_code == 200
    with factory() as db:
        db.query(OperationLog).delete(); db.commit()
    assert client.delete(path,params=_params(pub,draft)).status_code == 409

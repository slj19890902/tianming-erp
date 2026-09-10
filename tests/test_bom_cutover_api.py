import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api import bom_cutover as api
from app.api.deps import get_db
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from tests.test_bom_cutover_review import prepare
from tests.test_bom_cutover_writer import facts
from tests.test_multilevel_bom_factory_compile import factory_copy


@pytest.fixture
def client(factory_copy):
    db = factory_copy
    prepare(db)
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/orders")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[api.can_edit] = lambda: actor
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http, db, actor


URL = "/api/orders/items/10050/reserved-kit-cutover"


def reviewed(http):
    response = http.post(URL + "/preview", json={"target_locations": {"3799": 1203}})
    assert response.status_code == 200, response.text
    data = response.json()
    return {key: data[key] for key in ("target_locations", "source_lot_versions", "reviewed_hash", "preview_hash")} | {
        "operation_key": "http-test-conversion"}


def test_preview_execute_replay_audit(client):
    http, db, _ = client
    before = facts(db)
    initial = http.post(URL + "/preview", json={})
    assert initial.status_code == 200, initial.text
    assert initial.json()["ready"] is False
    assert initial.json()["execution_quantity"] == 300
    costs = initial.json()["source_costs"]
    assert sum(row["quantity"] for row in costs) == 2100
    assert all(row["lineage"]["actual"] is False for row in costs)
    assert "不重复计价" in initial.json()["cost_impact"]
    assert "不新增或修改采购" in initial.json()["procurement_impact"]
    assert "原子件不再重复出库" in initial.json()["picking_impact"]
    body = reviewed(http)
    assert facts(db) == before
    response = http.post(URL + "/execute", json=body)
    assert response.status_code == 200, response.text
    after = facts(db)
    assert http.post(URL + "/execute", json=body).json() == response.json()
    assert facts(db) == after
    assert db.execute(text("SELECT count(*) FROM operation_logs WHERE action_code='convert_legacy_bom_execution'")).scalar() == 1


@pytest.mark.parametrize("failure", ["stock", "map", "target", "audit", "permission"])
def test_http_failures_rollback(client, monkeypatch, failure):
    http, db, actor = client
    body = reviewed(http)
    if failure == "stock":
        db.get(InventoryLot, 365).version += 1
        db.commit()
    elif failure == "map":
        from app.services.warehouse_twin_layout import resolve_warehouse_twin_layout_path
        path = resolve_warehouse_twin_layout_path()
        # Same map semantics but a new published file invalidates the preview.
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif failure == "target":
        body["target_locations"] = {"3799": 656}
    elif failure == "permission":
        actor.role = "sales"
        db.commit()
    else:
        import app.services.multilevel_bom_cutover as writer
        def fail(*args, **kwargs):
            raise RuntimeError("audit failure")
        monkeypatch.setattr(writer, "append_audit_event", fail)
    before = facts(db)
    response = http.post(URL + "/execute", json=body)
    assert response.status_code == (500 if failure == "audit" else 403 if failure == "permission" else 409), response.text
    assert facts(db) == before


@pytest.mark.parametrize("kind", ["accompany", "body", "purchased"])
def test_unsupported_paths_rejected_in_preview(client, kind):
    http, db, actor = client
    from tests.test_multilevel_bom_master import save
    mode = "purchased" if kind == "purchased" else "manufactured"
    relation = "assembly" if kind == "body" else "accompany"
    save(db, actor, 3799, mode, [(3771, 3, relation), (3783, 4, relation)])
    db.commit()
    before = facts(db)
    response = http.post(URL + "/preview", json={})
    assert response.status_code == 409
    assert ("外购资料和采购比例" if kind == "purchased" else "本体、配套或采购来源交接") in response.json()["detail"]
    assert facts(db) == before


def test_unauthenticated_endpoint_is_denied():
    app = FastAPI()
    app.include_router(api.router, prefix="/api/orders")
    with TestClient(app, raise_server_exceptions=False) as http:
        assert http.post(URL + "/preview", json={}).status_code == 401


def test_router_is_mounted_on_real_orders_api():
    from app.api.orders import router
    assert "/items/{item_id}/reserved-kit-cutover/preview" in {r.path for r in router.routes}

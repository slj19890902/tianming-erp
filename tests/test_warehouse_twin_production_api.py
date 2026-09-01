from __future__ import annotations

from collections.abc import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import warehouse
from app.api.deps import get_db
from app.models.user import User


def _user() -> User:
    return User(
        id=11,
        username="twin-workshop",
        password_hash="unused",
        role="workshop",
        real_name="车间验收员",
        customer_access_mode="selected",
        must_change_password=False,
    )


def test_twin_production_read_requires_both_warehouse_and_order_permissions(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(warehouse.router, prefix="/api/warehouse")

    def override_db() -> Generator[object, None, None]:
        yield object()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        warehouse,
        "load_warehouse_twin_floor",
        lambda _floor: {"layout_id": "layout-1f", "floor_code": "1F"},
    )
    monkeypatch.setattr(
        warehouse,
        "overlay_formal_area_bindings",
        lambda _db, *, floor_code, floor_layout, include_draft=False: floor_layout,
    )
    monkeypatch.setattr(warehouse, "_current_visible_production_tasks", lambda *_args: [])
    monkeypatch.setattr(warehouse, "_visible_production_task_ids", lambda *_args: set())
    monkeypatch.setattr(warehouse, "_production_task_dates", lambda *_args: {})
    monkeypatch.setattr(
        warehouse,
        "build_production_projection",
        lambda **_kwargs: {
            "available": True,
            "items": [],
            "stale_mappings": [],
            "source_read_only": True,
        },
    )

    with TestClient(app) as client:
        assert client.get("/api/warehouse/twin-production/layouts/layout-1f/tasks").status_code == 401
        app.dependency_overrides[warehouse.can_read] = _user
        assert client.get("/api/warehouse/twin-production/layouts/layout-1f/tasks").status_code == 401
        app.dependency_overrides[warehouse.can_read_orders] = _user
        response = client.get("/api/warehouse/twin-production/layouts/layout-1f/tasks")
    assert response.status_code == 200
    assert response.json()["source_read_only"] is True


def test_twin_production_reuses_current_users_customer_scope(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(warehouse, "_visible_customer_ids", lambda _user, _db: {3, 7})

    def fake_list(_db, *, allowed_customer_ids, status):
        captured["allowed_customer_ids"] = allowed_customer_ids
        captured["status"] = status
        return []

    monkeypatch.setattr(warehouse, "list_production_tasks", fake_list)
    assert warehouse._current_visible_production_tasks(_user(), object()) == []
    assert captured == {"allowed_customer_ids": {3, 7}, "status": "pending"}

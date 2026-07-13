from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def access_control_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import (
        PermissionChecker,
        get_db,
        require_customer_access,
    )
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "access-control.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="admin",
                    password_hash=hash_password("AdminPass123!"),
                    role="admin",
                    real_name="Admin",
                    must_change_password=False,
                ),
                User(
                    username="boss",
                    password_hash=hash_password("BossPass123!"),
                    role="boss",
                    real_name="Boss",
                    must_change_password=False,
                ),
                User(
                    username="sales",
                    password_hash=hash_password("SalesPass123!"),
                    role="sales",
                    real_name="Sales",
                    must_change_password=False,
                ),
                Customer(name="Customer A"),
                Customer(name="Customer B"),
            ]
        )
        db.commit()
        ids = {
            user.username: user.id
            for user in db.query(User).order_by(User.username).all()
        }
        customer_ids = [
            customer.id for customer in db.query(Customer).order_by(Customer.id).all()
        ]

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")

    @app.get("/permission/orders-create")
    def orders_create(
        _user=Depends(PermissionChecker("orders.create")),
    ) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/customer/{customer_id}/protected")
    def customer_protected(
        customer_id: int,
        _user=Depends(require_customer_access),
    ) -> dict[str, int]:
        return {"customer_id": customer_id}

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids, customer_ids, engine
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()


def test_sales_defaults_are_business_limited_and_boss_excludes_backup(
    access_control_app,
) -> None:
    from app.api.deps import SALES_DEFAULT_PERMISSIONS, has_permission
    from app.models.user import User

    app, factory, ids, _customer_ids, _engine = access_control_app
    with factory() as db:
        sales = db.get(User, ids["sales"])
        boss = db.get(User, ids["boss"])
        assert SALES_DEFAULT_PERMISSIONS == frozenset(
            {
                "customers.view",
                "customers.edit",
                "products.view",
                "products.edit",
                "quotations.view",
                "quotations.edit",
                "orders.view",
                "orders.create",
                "orders.edit",
                "dashboard.view",
            }
        )
        assert has_permission(sales, "customers.edit")
        assert has_permission(sales, "products.edit")
        assert has_permission(sales, "quotations.view")
        assert has_permission(sales, "quotations.edit")
        assert has_permission(sales, "orders.create")
        assert has_permission(sales, "orders.edit")
        for permission in (
            "customers.deactivate",
            "customers.delete",
            "products.deactivate",
            "products.delete",
            "orders.status",
            "orders.delete",
            "orders.rollback",
            "incoming.view",
            "incoming.execute",
            "warehouse.view",
            "warehouse.reserve",
            "deliveries.view",
            "deliveries.execute",
            "requisition.view",
            "requisition.execute",
            "finance.view",
            "finance.execute",
            "cost.view",
            "system.backup",
            "users.manage",
        ):
            assert not has_permission(sales, permission)
        assert has_permission(boss, "orders.rollback")
        assert not has_permission(boss, "users.manage")
        assert not has_permission(boss, "system.backup")

    with TestClient(app) as client:
        boss_payload = _login(client, "boss", "BossPass123!")
        assert "orders.rollback" in boss_payload["permissions"]
        assert "system.backup" not in boss_payload["permissions"]
        assert client.get("/api/auth/users").status_code == 403


def test_admin_saves_user_overrides_and_customer_scope(access_control_app) -> None:
    app, _factory, ids, customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        created = client.post(
            "/api/auth/users",
            json={
                "username": "new-sales",
                "password": "NewSales123!",
                "role": "sales",
                "real_name": "New Sales",
            },
        )
        assert created.status_code == 201
        created_id = created.json()["user"]["id"]
        updated = client.put(
            f"/api/auth/users/{created_id}",
            json={"display_name": "Sales Person", "is_active": False},
        )
        assert updated.status_code == 200
        assert updated.json()["user"]["display_name"] == "Sales Person"

        saved_overrides = client.put(
            f"/api/auth/users/{ids['sales']}/permission-overrides",
            json={"overrides": {"orders.create": False, "cost.view": True}},
        )
        assert saved_overrides.status_code == 200
        assert "orders.create" not in saved_overrides.json()["effective_permissions"]
        assert "cost.view" in saved_overrides.json()["effective_permissions"]
        assert client.get(
            f"/api/auth/users/{ids['sales']}/permission-overrides"
        ).json()["overrides"] == [
            {"permission_code": "cost.view", "is_allowed": True},
            {"permission_code": "orders.create", "is_allowed": False},
        ]

        saved_scopes = client.put(
            f"/api/auth/users/{ids['sales']}/customer-scopes",
            json={"mode": "selected", "customer_ids": [customer_ids[0]]},
        )
        assert saved_scopes.status_code == 200
        assert saved_scopes.json()["customer_scope"] == [customer_ids[0]]

        client.post("/api/auth/logout")
        sales_payload = _login(client, "sales", "SalesPass123!")
        assert sales_payload["customer_scope"] == [customer_ids[0]]
        assert sales_payload["customer_access_mode"] == "selected"
        assert client.get("/permission/orders-create").status_code == 403
        assert client.get(f"/customer/{customer_ids[0]}/protected").status_code == 200
        assert client.get(f"/customer/{customer_ids[1]}/protected").status_code == 403


def test_selected_customer_scope_can_explicitly_deny_all_customers(
    access_control_app,
) -> None:
    app, _factory, ids, customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        saved = client.put(
            f"/api/auth/users/{ids['sales']}/customer-scopes",
            json={"mode": "selected", "customer_ids": []},
        )
        assert saved.status_code == 200
        assert saved.json()["customer_access_mode"] == "selected"
        assert saved.json()["unrestricted_customer_access"] is False

        client.post("/api/auth/logout")
        payload = _login(client, "sales", "SalesPass123!")
        assert payload["customer_access_mode"] == "selected"
        assert payload["unrestricted_customer_access"] is False
        assert client.get(f"/customer/{customer_ids[0]}/protected").status_code == 403


def test_atomic_access_update_rejects_invalid_scope_without_partial_save(
    access_control_app,
) -> None:
    app, _factory, ids, customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        saved = client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={
                "overrides": {"cost.view": True},
                "mode": "selected",
                "customer_ids": [customer_ids[0]],
            },
        )
        assert saved.status_code == 200

        rejected = client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={
                "overrides": {"orders.create": False},
                "mode": "selected",
                "customer_ids": [999999],
            },
        )
        assert rejected.status_code == 404
        current = client.get(
            f"/api/auth/users/{ids['sales']}/permission-overrides"
        ).json()
        assert current["overrides"] == [
            {"permission_code": "cost.view", "is_allowed": True}
        ]


def test_admin_cannot_demote_or_deactivate_self(access_control_app) -> None:
    app, _factory, ids, _customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        assert client.put(
            f"/api/auth/users/{ids['admin']}", json={"role": "sales"}
        ).status_code == 400
        assert client.put(
            f"/api/auth/users/{ids['admin']}", json={"is_active": False}
        ).status_code == 400


def test_admin_only_permissions_cannot_be_granted_to_business_roles(
    access_control_app,
) -> None:
    app, _factory, ids, _customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        response = client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={
                "overrides": {"users.manage": True, "system.backup": True},
                "mode": "all",
                "customer_ids": [],
            },
        )
        assert response.status_code == 400
        catalog = client.get(
            f"/api/auth/users/{ids['sales']}/permission-overrides"
        ).json()["permissions"]
        locked = {row["code"] for row in catalog if row["locked"]}
        assert {"users.manage", "system.backup"}.issubset(locked)


def test_access_control_models_have_unique_constraints(access_control_app) -> None:
    _app, _factory, _ids, _customer_ids, engine = access_control_app
    inspector = inspect(engine)
    overrides = inspector.get_unique_constraints("user_permission_overrides")
    scopes = inspector.get_unique_constraints("user_customer_scopes")
    assert frozenset({"user_id", "permission_code"}) in {
        frozenset(item["column_names"]) for item in overrides
    }
    assert frozenset({"user_id", "customer_id"}) in {
        frozenset(item["column_names"]) for item in scopes
    }


def test_n028_migration_has_required_revision_link() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "aj37v7w8x9f27_n028_access_control_core.py"
    )
    source = migration_path.read_text(encoding="utf-8")
    assert 'down_revision = "ai36v7w8x9e26"' in source
    assert "user_permission_overrides" in source
    assert "user_customer_scopes" in source

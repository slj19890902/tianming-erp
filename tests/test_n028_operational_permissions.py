from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


OPERATIONAL_PERMISSIONS = {
    "dashboard.view",
    "deliveries.view",
    "deliveries.execute",
    "finance.view",
    "finance.execute",
    "incoming.view",
    "incoming.execute",
    "warehouse.view",
    "warehouse.execute",
    "warehouse.reserve",
    "cost.view",
}


def test_operational_permission_defaults_preserve_role_access() -> None:
    from app.api.deps import has_permission
    from app.models.user import User

    expected = {
        "admin": OPERATIONAL_PERMISSIONS,
        "boss": OPERATIONAL_PERMISSIONS - {"finance.execute"},
        "sales": {"dashboard.view"},
        "finance": {
            "dashboard.view",
            "deliveries.view",
            "finance.view",
            "finance.execute",
            "cost.view",
        },
        "workshop": {
            "dashboard.view",
            "deliveries.view",
            "incoming.view",
            "incoming.execute",
            "warehouse.view",
            "warehouse.execute",
        },
        "delivery_picker": set(),
    }

    for role, permission_codes in expected.items():
        user = User(role=role)
        user.permission_overrides = []
        actual = {
            code for code in OPERATIONAL_PERMISSIONS if has_permission(user, code)
        }
        assert actual == permission_codes


@pytest.fixture()
def scoped_operational_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import router as deliveries_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n028-operational-permissions.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n028-admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="N028 Admin",
            must_change_password=False,
        )
        user = User(
            username="n028-scoped-sales",
            password_hash=hash_password("ScopedSales123!"),
            role="sales",
            real_name="Scoped Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(name="Scoped Customer A")
        customer_b = Customer(name="Scoped Customer B")
        db.add_all([admin, user, customer_a, customer_b])
        db.flush()
        delivery_a = Delivery(
            delivery_number="DH-N028-A",
            customer_id=customer_a.id,
            delivery_date=date(2026, 7, 13),
            status="pending",
            total_quantity=0,
            created_by=user.id,
        )
        delivery_b = Delivery(
            delivery_number="DH-N028-B",
            customer_id=customer_b.id,
            delivery_date=date(2026, 7, 13),
            status="pending",
            total_quantity=0,
            created_by=user.id,
        )
        db.add_all(
            [
                UserCustomerScope(user_id=user.id, customer_id=customer_a.id),
                delivery_a,
                delivery_b,
            ]
        )
        db.commit()
        ids = {
            "sales_user": user.id,
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "delivery_a": delivery_a.id,
            "delivery_b": delivery_b.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids
    finally:
        engine.dispose()


def test_selected_customer_scope_filters_and_rejects_operational_access(
    scoped_operational_app,
) -> None:
    app, ids = scoped_operational_app
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={
                "username": "n028-scoped-sales",
                "password": "ScopedSales123!",
            },
        )
        assert login.status_code == 200

        assert client.get("/api/deliveries").status_code == 403
        assert client.get("/api/warehouse/references/customers").status_code == 403

        client.post("/api/auth/logout")
        admin_login = client.post(
            "/api/auth/login",
            json={"username": "n028-admin", "password": "AdminPass123!"},
        )
        assert admin_login.status_code == 200
        granted = client.put(
            f"/api/auth/users/{ids['sales_user']}/permission-overrides",
            json={
                "overrides": {
                    "deliveries.view": True,
                    "deliveries.execute": True,
                    "warehouse.view": True,
                }
            },
        )
        assert granted.status_code == 200
        assert {
            "deliveries.view",
            "deliveries.execute",
            "warehouse.view",
        }.issubset(granted.json()["effective_permissions"])

        client.post("/api/auth/logout")
        login = client.post(
            "/api/auth/login",
            json={
                "username": "n028-scoped-sales",
                "password": "ScopedSales123!",
            },
        )
        assert login.status_code == 200

        listing = client.get("/api/deliveries")
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [
            ids["delivery_a"]
        ]
        assert client.get(f"/api/deliveries/{ids['delivery_b']}").status_code == 403

        customers = client.get("/api/warehouse/references/customers")
        assert customers.status_code == 200
        assert [item["id"] for item in customers.json()["items"]] == [
            ids["customer_a"]
        ]
        assert (
            client.get(
                "/api/warehouse/references/products",
                params={"customer_id": ids["customer_b"]},
            ).status_code
            == 403
        )

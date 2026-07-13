from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def sensitive_permissions_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.quotations import router as quotations_router
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n028-sensitive-permissions.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n028-admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="Admin",
            must_change_password=False,
        )
        sales = User(
            username="n028-sales",
            password_hash=hash_password("SalesPass123!"),
            role="sales",
            real_name="Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(name="Customer A")
        customer_b = Customer(name="Customer B")
        material = Material(
            code="A416D",
            layer_count=5,
            supplier_name="Board Supplier",
            quote_price=Decimal("5.0000"),
            is_active=True,
        )
        db.add_all([admin, sales, customer_a, customer_b, material])
        db.flush()
        db.add(UserCustomerScope(user_id=sales.id, customer_id=customer_a.id))
        db.commit()
        ids = {
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "material": material.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(quotations_router, prefix="/api/quotations")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, engine
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def _quotation_payload(material_id: int) -> dict:
    return {
        "items": [
            {
                "product_name": "N028 carton",
                "box_type": "A1/0201",
                "length_mm": 300,
                "width_mm": 200,
                "height_mm": 150,
                "material_id": material_id,
                "flute_type": "AB",
                "quantity": 100,
                "margin_rate": 20,
                "final_unit_price": 2.5,
            }
        ],
    }


def test_sales_quotation_responses_are_redacted_and_customer_scoped(
    sensitive_permissions_app,
) -> None:
    app, ids, _engine = sensitive_permissions_app
    with TestClient(app) as client:
        _login(client, "n028-admin", "AdminPass123!")
        first = client.post(
            f"/api/quotations?customer_id={ids['customer_a']}",
            json=_quotation_payload(ids["material"]),
        )
        assert first.status_code == 201
        assert "estimated_unit_cost" in first.json()["items"][0]

        second = client.post(
            f"/api/quotations?customer_id={ids['customer_b']}",
            json=_quotation_payload(ids["material"]),
        )
        assert second.status_code == 201
        client.post("/api/auth/logout")

        _login(client, "n028-sales", "SalesPass123!")
        preview = client.post(
            "/api/quotations/preview",
            json={
                "box_type": "A1/0201",
                "length_mm": 300,
                "width_mm": 200,
                "height_mm": 150,
                "material_id": ids["material"],
                "flute_type": "AB",
            },
        )
        assert preview.status_code == 200
        assert {
            "estimated_unit_cost",
            "estimated_gross_profit",
            "margin_rate",
            "material_square_price",
            "suggested_unit_price",
        }.isdisjoint(preview.json())

        listing = client.get("/api/quotations")
        assert listing.status_code == 200
        assert [row["id"] for row in listing.json()["items"]] == [first.json()["id"]]
        item = listing.json()["items"][0]["items"][0]
        assert {
            "estimated_unit_cost",
            "estimated_gross_profit",
            "margin_rate",
            "material_square_price",
            "suggested_unit_price",
        }.isdisjoint(item)

        assert client.get(f"/api/quotations/{second.json()['id']}").status_code == 403
        assert (
            client.put(
                f"/api/quotations/{second.json()['id']}",
                json=_quotation_payload(ids["material"]),
            ).status_code
            == 403
        )


def test_sales_cannot_read_or_execute_requisition_apis(
    sensitive_permissions_app,
) -> None:
    app, _ids, _engine = sensitive_permissions_app
    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        assert client.get("/api/requisition/pending").status_code == 403
        assert client.put("/api/requisition/supplier-orders/1/void").status_code == 403

        client.post("/api/auth/logout")
        _login(client, "n028-admin", "AdminPass123!")
        assert client.get("/api/requisition/pending").status_code == 200

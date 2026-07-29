from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.orders import router as orders_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User


PASSWORD = "123456"


@pytest.fixture()
def order_filter_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-06-orders.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="p106-order-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="P1-06 Admin",
            must_change_password=False,
            customer_access_mode="all",
        )
        scoped = User(
            username="p106-order-scoped",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="P1-06 Scoped",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(name="筛选客户甲")
        customer_b = Customer(name="筛选客户乙")
        db.add_all([admin, scoped, customer_a, customer_b])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(
                    user_id=scoped.id,
                    customer_id=customer_a.id,
                    assigned_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="orders.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
            ]
        )

        def add_order(
            *,
            key: str,
            customer: Customer,
            code: str,
            name: str,
            spec: str,
            status: str,
            created_at: datetime,
        ) -> int:
            product = Product(
                customer_id=customer.id,
                product_code=code,
                customer_material_code=f"MAT-{key}",
                product_name=name,
                box_style="普通箱",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"ORD-{key}",
                customer_id=customer.id,
                customer_po=f"PO-{key}",
                order_date=date(2026, 7, 20 if key == "A1" else 21),
                delivery_date=date(2026, 7, 28 if key == "A1" else 29),
                status=status,
                payment_status="unpaid",
                total_amount=Decimal("10"),
                created_at=created_at,
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    item_sequence=1,
                    item_order_number=f"{order.order_number}-001",
                    quantity=10,
                    delivered_quantity=0,
                    unit_price=Decimal("1"),
                    subtotal=Decimal("10"),
                    material_status="received",
                    snapshot_product_code=code,
                    snapshot_product_name=name,
                    snapshot_spec=spec,
                )
            )
            db.flush()
            return order.id

        ids = {
            "a1": add_order(
                key="A1",
                customer=customer_a,
                code="BX-100",
                name="甲款外箱",
                spec="400x300x200",
                status="pending_delivery",
                created_at=datetime(2026, 7, 20, 8, 0),
            ),
            "a2": add_order(
                key="A2",
                customer=customer_a,
                code="BX-101",
                name="甲款内盒",
                spec="500x400x300",
                status="production",
                created_at=datetime(2026, 7, 21, 8, 0),
            ),
            "b": add_order(
                key="B1",
                customer=customer_b,
                code="BX-200",
                name="乙款外箱",
                spec="400x300x200",
                status="pending_confirmation",
                created_at=datetime(2026, 7, 22, 8, 0),
            ),
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
        }
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def test_order_list_filters_paginate_and_preserve_old_customer_id(order_filter_app) -> None:
    app, ids = order_filter_app
    with TestClient(app) as client:
        _login(client, "p106-order-admin")
        old_customer = client.get(f"/api/orders?customer_id={ids['customer_a']}")
        combined = client.get(
            "/api/orders?customer_ids="
            f"{ids['customer_a']}&customer_ids={ids['customer_b']}&customer_po=PO-A2"
            "&product_code=BX-101&product_name=%E5%86%85%E7%9B%92"
            "&specification=500x400&order_date_from=2026-07-21"
            "&order_date_to=2026-07-21&delivery_date_from=2026-07-29"
            "&delivery_date_to=2026-07-29&status=production&page=1&page_size=1"
        )
        page_one = client.get("/api/orders?page=1&page_size=1")
        page_two = client.get("/api/orders?page=2&page_size=1")
        multi_status = client.get(
            "/api/orders?status=production&status=pending_confirmation&page=1&page_size=50"
        )

    assert old_customer.status_code == 200
    assert {row["id"] for row in old_customer.json()["items"]} == {ids["a1"], ids["a2"]}
    assert combined.status_code == 200, combined.text
    assert combined.json()["total"] == 1
    assert [row["id"] for row in combined.json()["items"]] == [ids["a2"]]
    assert page_one.json()["items"][0]["id"] == ids["b"]
    assert page_two.json()["items"][0]["id"] == ids["a2"]
    assert {row["id"] for row in multi_status.json()["items"]} == {
        ids["a2"], ids["b"]
    }


def test_order_list_rejects_out_of_scope_customer_ids(order_filter_app) -> None:
    app, ids = order_filter_app
    with TestClient(app) as client:
        _login(client, "p106-order-scoped")
        allowed = client.get("/api/orders?page=1&page_size=50")
        denied = client.get(
            f"/api/orders?customer_ids={ids['customer_a']}&customer_ids={ids['customer_b']}"
        )

    assert allowed.status_code == 200, allowed.text
    assert {row["id"] for row in allowed.json()["items"]} == {ids["a1"], ids["a2"]}
    assert denied.status_code == 403

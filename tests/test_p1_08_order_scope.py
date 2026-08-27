from __future__ import annotations

from collections.abc import Generator
from datetime import date
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
def order_scope_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-08-order-scope.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="p108-admin", password_hash=hash_password(PASSWORD), role="admin",
            real_name="P1-08 Admin", must_change_password=False, customer_access_mode="all",
        )
        scoped = User(
            username="p108-scoped", password_hash=hash_password(PASSWORD), role="sales",
            real_name="P1-08 Scoped", must_change_password=False, customer_access_mode="selected",
        )
        customer_a = Customer(customer_number=1, customer_code="A", name="有单客户甲", is_active=False)
        customer_b = Customer(customer_number=2, customer_code="B", name="有单客户乙")
        no_order = Customer(customer_number=3, customer_code="EMPTY", name="无订单客户")
        db.add_all([admin, scoped, customer_a, customer_b, no_order])
        db.flush()
        db.add_all([
            UserCustomerScope(user_id=scoped.id, customer_id=customer_a.id, assigned_by=admin.id),
            UserPermissionOverride(user_id=scoped.id, permission_code="orders.view", is_allowed=True, granted_by=admin.id),
        ])

        def add_order(customer: Customer, *, number: str, status: str) -> int:
            product = Product(customer_id=customer.id, product_code=f"P-{number}", customer_material_code=f"M-{number}", product_name="测试箱", box_style="普通箱")
            db.add(product)
            db.flush()
            order = Order(order_number=number, customer_id=customer.id, order_date=date(2026, 7, 29), delivery_date=date(2026, 7, 30), status=status, payment_status="unpaid", total_amount=Decimal("1"))
            db.add(order)
            db.flush()
            db.add(OrderItem(order_id=order.id, product_id=product.id, item_sequence=1, item_order_number=f"{number}-001", quantity=1, delivered_quantity=0, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending", snapshot_product_code=product.product_code, snapshot_product_name=product.product_name))
            db.flush()
            return order.id

        ids = {
            "a": customer_a.id,
            "b": customer_b.id,
            "empty": no_order.id,
            "active_a": add_order(customer_a, number="P108-A", status="production"),
            "active_b": add_order(customer_b, number="P108-B-ACTIVE", status="production"),
            "terminal_b": add_order(customer_b, number="P108-B-CLOSED", status="closed"),
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
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text


def test_customer_options_only_lists_in_scope_customers_with_orders(order_scope_app) -> None:
    app, ids = order_scope_app
    with TestClient(app) as client:
        _login(client, "p108-admin")
        active = client.get("/api/orders/customer-options")
        all_scope = client.get("/api/orders/customer-options?scope=all&page=1&page_size=1")
        cancelled = client.get("/api/orders/customer-options?scope=cancelled")
        keyword = client.get("/api/orders/customer-options?keyword=%E7%94%B2")

    assert active.status_code == 200
    assert {row["id"] for row in active.json()["items"]} == {ids["a"], ids["b"]}
    assert next(row for row in active.json()["items"] if row["id"] == ids["a"])["is_active"] is False
    assert all_scope.json()["total"] == 2
    assert len(all_scope.json()["items"]) == 1
    assert [row["id"] for row in cancelled.json()["items"]] == [ids["b"]]
    assert [row["id"] for row in keyword.json()["items"]] == [ids["a"]]
    assert ids["empty"] not in {row["id"] for row in all_scope.json()["items"]}


def test_scope_and_stage_are_fixed_before_keyword_and_customer_scope(order_scope_app) -> None:
    app, ids = order_scope_app
    with TestClient(app) as client:
        _login(client, "p108-admin")
        without_keyword = client.get("/api/orders?scope=active&status=business&page=1&page_size=50")
        with_keyword = client.get("/api/orders?scope=active&status=business&keyword=P108-A&page=1&page_size=50")
        stage = client.get("/api/orders?scope=active&stage=pending_material&page=1&page_size=50")
        _login(client, "p108-scoped")
        scoped = client.get("/api/orders/customer-options?scope=all")

    assert {row["id"] for row in without_keyword.json()["items"]} == {ids["active_a"], ids["active_b"]}
    assert {row["id"] for row in with_keyword.json()["items"]} == {ids["active_a"]}
    assert {row["id"] for row in stage.json()["items"]} == {ids["active_a"], ids["active_b"]}
    assert [row["id"] for row in scoped.json()["items"]] == [ids["a"]]


def test_legacy_completed_status_maps_to_completed_scope(order_scope_app) -> None:
    app, _ = order_scope_app
    with TestClient(app) as client:
        _login(client, "p108-admin")
        response = client.get("/api/orders?status=completed&page=1&page_size=50")

    assert response.status_code == 200
    assert response.json()["total"] == 0

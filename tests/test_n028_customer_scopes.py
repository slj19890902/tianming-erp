from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def n028_customer_scope_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.customers import router as customers_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.product import Product
    from app.models.product_drawing import ProductDrawing
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n028-customer-scopes.sqlite3")
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
        boss = User(
            username="n028-boss",
            password_hash=hash_password("BossPass123!"),
            role="boss",
            real_name="Boss",
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
        empty_sales = User(
            username="n028-empty-sales",
            password_hash=hash_password("EmptySalesPass123!"),
            role="sales",
            real_name="Empty Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(
            customer_number=1,
            customer_code="N028-A",
            name="N028 Customer",
        )
        other_customer = Customer(
            customer_number=2,
            customer_code="N028-B",
            name="N028 Other Customer",
        )
        db.add_all([admin, boss, sales, empty_sales, customer, other_customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="N028-BASE",
            customer_material_code="N028-BASE",
            product_name="N028 base carton",
            box_category="normal",
            sale_unit_price=Decimal("8.00"),
            cost_unit_price=Decimal("2.00"),
            board_price=Decimal("3.00"),
            suggested_price=Decimal("4.00"),
        )
        db.add_all(
            [
                product,
                Product(
                    customer_id=other_customer.id,
                    product_code="N028-OTHER",
                    customer_material_code="N028-OTHER",
                    product_name="N028 other carton",
                    box_category="normal",
                ),
                UserCustomerScope(user_id=sales.id, customer_id=customer.id),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="products.create",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                Order(
                    order_number="N028-SCOPE-ORDER",
                    customer_id=customer.id,
                    order_date=date(2026, 7, 13),
                    total_amount=Decimal("0"),
                    created_by=admin.id,
                ),
                Order(
                    order_number="N028-OTHER-ORDER",
                    customer_id=other_customer.id,
                    order_date=date(2026, 7, 13),
                    total_amount=Decimal("0"),
                    created_by=admin.id,
                ),
            ]
        )
        db.flush()
        db.add(
            ProductDrawing(
                product_id=product.id,
                image_path="/static/uploads/drawings/legacy.png",
                thumbnail_path="/static/uploads/drawings/legacy.png",
                uploaded_by=admin.id,
            )
        )
        db.commit()
        ids = {
            "customer": customer.id,
            "other_customer": other_customer.id,
            "product": product.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(customers_router, prefix="/api/master/customers")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def _product_payload(customer_id: int, *, code: str, name: str) -> dict:
    return {
        "customer_id": customer_id,
        "product_code": code,
        "customer_material_code": code,
        "product_name": name,
        "box_category": "normal",
        "sale_unit_price": "9.00",
        "cost_unit_price": "20.00",
        "board_price": "21.00",
        "suggested_price": "22.00",
    }


def _overwrite_order_payload(customer_id: int, product_id: int) -> dict:
    return {
        "customer_id": customer_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "unit_price": "9.00",
                "temp_drawing_file": "/static/uploads/drawings/replacement.png",
                "drawing_save_option": "overwrite_product",
            }
        ],
    }


def test_empty_selected_scope_returns_no_customer_product_or_order_rows(
    n028_customer_scope_app,
) -> None:
    app, ids, _factory = n028_customer_scope_app
    with TestClient(app) as client:
        _login(client, "n028-empty-sales", "EmptySalesPass123!")
        customers = client.get("/api/master/customers")
        products = client.get("/api/master/products")
        orders = client.get("/api/orders")
        directed_product_list = client.get(
            "/api/master/products", params={"customer_id": ids["customer"]}
        )

    assert customers.json()["items"] == []
    assert products.json()["items"] == []
    assert orders.json()["items"] == []
    assert directed_product_list.status_code == 403

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        selected_customers = client.get("/api/master/customers")
        selected_products = client.get("/api/master/products")
        selected_orders = client.get("/api/orders")
        assert client.get(
            "/api/master/products", params={"customer_id": ids["other_customer"]}
        ).status_code == 403
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        all_customers = client.get("/api/master/customers")
        all_products = client.get("/api/master/products")
        all_orders = client.get("/api/orders")

    assert [item["id"] for item in selected_customers.json()["items"]] == [
        ids["customer"]
    ]
    assert [item["customer_id"] for item in selected_products.json()["items"]] == [
        ids["customer"]
    ]
    assert [item["customer_id"] for item in selected_orders.json()["items"]] == [
        ids["customer"]
    ]
    assert {item["id"] for item in all_customers.json()["items"]} == {
        ids["customer"],
        ids["other_customer"],
    }
    assert {item["customer_id"] for item in all_products.json()["items"]} == {
        ids["customer"],
        ids["other_customer"],
    }
    assert {item["customer_id"] for item in all_orders.json()["items"]} == {
        ids["customer"],
        ids["other_customer"],
    }


def test_sales_cost_payload_is_ignored_while_admin_and_boss_can_write_costs(
    n028_customer_scope_app,
) -> None:
    from app.models.product import Product

    app, ids, factory = n028_customer_scope_app
    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        created = client.post(
            "/api/master/products",
            json=_product_payload(ids["customer"], code="N028-SALES", name="Sales carton"),
        )
        assert created.status_code == 201
        updated = client.put(
            f"/api/master/products/{ids['product']}",
            json={
                **_product_payload(ids["customer"], code="N028-BASE", name="Renamed carton"),
                "expected_version": 1,
                "change_reason": "业务员更新常用箱",
            },
        )
        assert updated.status_code == 200
        synced = client.post(
            f"/api/master/products/{ids['product']}/sync-fields",
            json={
                "fields": {
                    "product_name": "Synced carton",
                    "cost_unit_price": "99.00",
                    "board_price": "98.00",
                    "suggested_price": "97.00",
                },
                "expected_version": 2,
                "change_reason": "业务员同步常用箱",
            },
        )
        assert synced.status_code == 200
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        admin_created = client.post(
            "/api/master/products",
            json=_product_payload(ids["customer"], code="N028-ADMIN", name="Admin carton"),
        )
        assert admin_created.status_code == 201
        client.post("/api/auth/logout")

        _login(client, "n028-boss", "BossPass123!")
        boss_payload = _product_payload(
            ids["customer"], code="N028-ADMIN", name="Boss edited carton"
        )
        boss_payload["cost_unit_price"] = "30.00"
        boss_updated = client.put(
            f"/api/master/products/{admin_created.json()['id']}",
            json={**boss_payload, "expected_version": 1, "change_reason": "老板更新常用箱"},
        )
        assert boss_updated.status_code == 200

    with factory() as db:
        sales_created = db.scalar(
            select(Product).where(Product.product_code == "N028-SALES")
        )
        original = db.get(Product, ids["product"])
        admin_product = db.get(Product, admin_created.json()["id"])
        assert sales_created is not None
        assert (
            sales_created.cost_unit_price,
            sales_created.board_price,
            sales_created.suggested_price,
        ) == (None, None, None)
        assert sales_created.sale_unit_price == Decimal("9.00")
        assert (
            original.cost_unit_price,
            original.board_price,
            original.suggested_price,
            original.product_name,
        ) == (
            Decimal("2.00"),
            Decimal("3.00"),
            Decimal("4.00"),
            "Synced carton",
        )
        assert (
            admin_product.cost_unit_price,
            admin_product.board_price,
            admin_product.suggested_price,
        ) == (Decimal("30.00"), Decimal("21.00"), Decimal("22.00"))


def test_sales_cannot_overwrite_product_drawings_but_admin_can(
    n028_customer_scope_app,
) -> None:
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_customer_scope_app
    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        denied = client.post(
            "/api/orders", json=_overwrite_order_payload(ids["customer"], ids["product"])
        )
        assert denied.status_code == 403
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        allowed = client.post(
            "/api/orders", json=_overwrite_order_payload(ids["customer"], ids["product"])
        )
        assert allowed.status_code == 201

    with factory() as db:
        drawings = db.scalars(
            select(ProductDrawing).where(ProductDrawing.product_id == ids["product"])
        ).all()
        assert len(drawings) == 1
        assert drawings[0].image_path.endswith("replacement.png")
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 1

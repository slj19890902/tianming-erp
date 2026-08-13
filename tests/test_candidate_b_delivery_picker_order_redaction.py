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
from app.models.access_control import UserPermissionOverride
from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
from app.models.user import User


PASSWORD = "RolePass123!"
PASSWORD_HASH = hash_password(PASSWORD)
SALES_FIELDS = {"unit_price", "subtotal", "sale_amount"}


@pytest.fixture()
def picker_order_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "candidate-b-picker-orders.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        users = {
            role: User(
                username=f"candidate-b-{role}",
                password_hash=PASSWORD_HASH,
                role=role,
                real_name=f"Candidate B {role}",
                must_change_password=False,
                customer_access_mode="all",
            )
            for role in ("admin", "boss", "sales", "finance", "workshop")
        }
        default_picker = User(
            username="candidate-b-picker-default",
            password_hash=PASSWORD_HASH,
            role="delivery_picker",
            real_name="Candidate B picker default",
            must_change_password=False,
            customer_access_mode="all",
        )
        override_picker = User(
            username="candidate-b-picker-override",
            password_hash=PASSWORD_HASH,
            role="delivery_picker",
            real_name="Candidate B picker override",
            must_change_password=False,
            customer_access_mode="all",
        )
        cost_picker = User(
            username="candidate-b-picker-cost",
            password_hash=PASSWORD_HASH,
            role="delivery_picker",
            real_name="Candidate B picker cost",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(name="Candidate B scoped customer")
        material = Material(
            code="CANDIDATE-B-MATERIAL",
            quote_price=Decimal("1.2500"),
            supplier_name="Candidate B supplier",
            layer_count=3,
            flute_type="A",
            is_active=True,
        )
        db.add_all(
            [
                *users.values(),
                default_picker,
                override_picker,
                cost_picker,
                customer,
                material,
            ]
        )
        db.flush()
        db.add(
            SupplierFlutePriceRule(
                supplier_name=material.supplier_name,
                layer_count=3,
                flute_type="A",
                price_delta=Decimal("0.0300"),
                effective_date=date(2026, 1, 1),
                is_active=True,
            )
        )
        product = Product(
            customer_id=customer.id,
            product_code="CANDIDATE-B-BOX",
            customer_material_code="CANDIDATE-B-CUSTOMER-MATERIAL",
            product_name="Candidate B carton",
            material_id=material.id,
            box_style="0201",
            layer_count=3,
            flute_type="A",
            report_length_mm=500,
            report_width_mm=300,
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="TM-CANDIDATE-B-001",
            customer_id=customer.id,
            customer_po="PO-CANDIDATE-B",
            order_date=date(2026, 8, 13),
            delivery_date=date(2026, 8, 20),
            status="pending_confirmation",
            payment_status="unpaid",
            total_amount=Decimal("120.00"),
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_sequence=1,
                item_order_number="TM-CANDIDATE-B-001-001",
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("1.2000"),
                subtotal=Decimal("120.00"),
                material_status="pending",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="500x300",
                snapshot_material=material.code,
                material_id=material.id,
                snapshot_supplier_name=material.supplier_name,
                layer_count=3,
                flute_type="A",
                snapshot_report_length_mm=500,
                snapshot_report_width_mm=300,
                snapshot_pieces_per_box=1,
            )
        )
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=override_picker.id,
                    permission_code="orders.view",
                    is_allowed=True,
                    granted_by=users["admin"].id,
                ),
                UserPermissionOverride(
                    user_id=cost_picker.id,
                    permission_code="orders.view",
                    is_allowed=True,
                    granted_by=users["admin"].id,
                ),
                UserPermissionOverride(
                    user_id=cost_picker.id,
                    permission_code="cost.view",
                    is_allowed=True,
                    granted_by=users["admin"].id,
                ),
                UserPermissionOverride(
                    user_id=users["sales"].id,
                    permission_code="orders.view",
                    is_allowed=False,
                    granted_by=users["admin"].id,
                ),
            ]
        )
        db.commit()
        ids = {"customer": int(customer.id), "order": int(order.id)}

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


def _login(client: TestClient, username: str) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _logout(client: TestClient) -> None:
    response = client.post("/api/auth/logout")
    assert response.status_code == 200, response.text


def _full_reads(client: TestClient, ids: dict[str, int]) -> list[dict]:
    full_list = client.get(
        "/api/orders",
        params={"scope": "all", "detail_level": "full", "page": 1, "page_size": 25},
    )
    group = client.get(
        "/api/orders/group-detail",
        params={
            "customer_id": ids["customer"],
            "anchor_order_id": ids["order"],
            "customer_po": "PO-CANDIDATE-B",
            "scope": "all",
        },
    )
    single = client.get(f"/api/orders/{ids['order']}")
    for response in (full_list, group, single):
        assert response.status_code == 200, response.text
    return [
        full_list.json()["items"][0],
        group.json()["orders"][0],
        single.json(),
    ]


def _assert_sales_redacted(order: dict) -> None:
    assert "total_amount" not in order
    assert "payment_status" not in order
    assert order["items"]
    assert SALES_FIELDS.isdisjoint(order["items"][0])


def _assert_sales_visible(order: dict) -> None:
    assert Decimal(order["total_amount"]) == Decimal("120.00")
    assert order["payment_status"] == "unpaid"
    assert SALES_FIELDS.issubset(order["items"][0])


def test_delivery_picker_defaults_to_pick_only_and_all_order_reads_fail_closed(
    picker_order_app,
) -> None:
    app, ids = picker_order_app
    with TestClient(app) as client:
        login = _login(client, "candidate-b-picker-default")
        assert "deliveries.pick" in login["permissions"]
        assert "orders.view" not in login["permissions"]

        requests = [
            client.get("/api/orders"),
            client.get(
                "/api/orders",
                params={
                    "detail_level": "summary",
                    "customer_id": ids["customer"],
                    "keyword": "CANDIDATE-B",
                    "page": 1,
                    "page_size": 1,
                },
            ),
            client.get(
                "/api/orders/group-detail",
                params={
                    "customer_id": ids["customer"],
                    "anchor_order_id": ids["order"],
                    "customer_po": "PO-CANDIDATE-B",
                    "scope": "all",
                },
            ),
            client.get(f"/api/orders/{ids['order']}"),
        ]

    assert all(response.status_code == 403 for response in requests)


@pytest.mark.parametrize(
    "username",
    ["candidate-b-picker-override", "candidate-b-workshop"],
)
def test_non_sales_roles_with_order_read_access_are_redacted_on_every_shape(
    picker_order_app,
    username: str,
) -> None:
    app, ids = picker_order_app
    with TestClient(app) as client:
        login = _login(client, username)
        assert "orders.view" in login["permissions"]
        summary = client.get(
            "/api/orders",
            params={"scope": "all", "detail_level": "summary", "page": 1, "page_size": 25},
        )
        assert summary.status_code == 200, summary.text
        assert "total_amount" not in summary.json()["items"][0]
        for order in _full_reads(client, ids):
            _assert_sales_redacted(order)


@pytest.mark.parametrize("role", ["admin", "boss", "finance"])
def test_positive_sales_roles_keep_existing_order_amount_contract(
    picker_order_app,
    role: str,
) -> None:
    app, ids = picker_order_app
    with TestClient(app) as client:
        _login(client, f"candidate-b-{role}")
        summary = client.get(
            "/api/orders",
            params={"scope": "all", "detail_level": "summary", "page": 1, "page_size": 25},
        )
        assert summary.status_code == 200, summary.text
        assert Decimal(summary.json()["items"][0]["total_amount"]) == Decimal("120.00")
        for order in _full_reads(client, ids):
            _assert_sales_visible(order)


def test_sales_role_with_explicit_order_view_deny_cannot_recover_amounts(
    picker_order_app,
) -> None:
    app, ids = picker_order_app
    with TestClient(app) as client:
        login = _login(client, "candidate-b-sales")
        assert "orders.view" not in login["permissions"]
        assert client.get("/api/orders").status_code == 403
        assert client.get(f"/api/orders/{ids['order']}").status_code == 403


def test_cost_view_remains_independent_from_sales_amount_visibility(
    picker_order_app,
) -> None:
    app, ids = picker_order_app
    with TestClient(app) as client:
        login = _login(client, "candidate-b-picker-cost")
        assert {"orders.view", "cost.view"}.issubset(login["permissions"])
        for order in _full_reads(client, ids):
            _assert_sales_redacted(order)
            assert "estimated_cost" in order["items"][0]

        _logout(client)
        login = _login(client, "candidate-b-picker-override")
        assert "cost.view" not in login["permissions"]
        for order in _full_reads(client, ids):
            _assert_sales_redacted(order)
            assert "estimated_cost" not in order["items"][0]

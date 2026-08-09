from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

import app.api.orders as orders_api
from app.api.deps import get_db
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
from app.models.user import User
from app.services.requisition_quantities import DEFAULT_CUTTING_MODE


PAGE_CONTEXT_KEYS = {
    "finished_reservations_by_item_id",
    "external_purchase_summaries_by_order_id",
    "frozen_material_costs_by_item_id",
    "frozen_estimated_costs_by_item_id",
    "material_cost_context",
}


def _user(role: str) -> User:
    return User(
        id=-1,
        username=f"full-list-{role}",
        password_hash="unused",
        role=role,
        real_name=f"Full list {role}",
        is_active=True,
        must_change_password=False,
        customer_access_mode="all",
    )


@pytest.fixture()
def full_list_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "order-full-list.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(name="Full list customer")
        material = Material(
            code="FULL-LIST-MATERIAL",
            quote_price=Decimal("1.2500"),
            supplier_name="Full list supplier",
            layer_count=3,
            flute_type="A",
            is_active=True,
        )
        db.add_all([customer, material])
        db.flush()
        db.add_all(
            [
                SupplierFlutePriceRule(
                    supplier_name=material.supplier_name,
                    layer_count=3,
                    flute_type="A",
                    price_delta=Decimal("0.0300"),
                    effective_date=date(2026, 1, 1),
                    is_active=True,
                ),
                SupplierFlutePriceRule(
                    supplier_name=material.supplier_name,
                    layer_count=3,
                    flute_type="A",
                    price_delta=Decimal("0.0500"),
                    effective_date=date(2026, 8, 1),
                    is_active=True,
                ),
            ]
        )
        product = Product(
            customer_id=customer.id,
            product_code="FULL-LIST-PRODUCT",
            customer_material_code="FULL-LIST-CUSTOMER-MATERIAL",
            product_name="Full list carton",
            material_id=material.id,
            box_style="0201",
            layer_count=3,
            flute_type="A",
            report_length_mm=500,
            report_width_mm=300,
        )
        db.add(product)
        db.flush()
        for order_index in range(30):
            order = Order(
                order_number=f"TM-FULL-{order_index:03d}",
                customer_id=customer.id,
                customer_po=f"PO-FULL-{order_index:03d}",
                order_date=date(2026, 8, 1),
                delivery_date=date(2026, 8, 20),
                status="pending_confirmation",
                payment_status="unpaid",
                total_amount=Decimal("20.00"),
            )
            db.add(order)
            db.flush()
            for item_index in range(2):
                db.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=product.id,
                        item_sequence=item_index + 1,
                        item_order_number=(
                            f"TM-FULL-{order_index:03d}-{item_index + 1:03d}"
                        ),
                        quantity=10,
                        delivered_quantity=0,
                        unit_price=Decimal("1.0000"),
                        subtotal=Decimal("10.00"),
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
                        special_process=DEFAULT_CUTTING_MODE,
                    )
                )
        db.commit()

    app = FastAPI()
    app.include_router(orders_api.router, prefix="/api/orders")
    current_user = {"value": _user("admin")}

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[orders_api.can_read] = lambda: current_user["value"]
    try:
        yield app, engine, current_user
    finally:
        engine.dispose()


def _request(client: TestClient, *, page_size: int = 25):
    return client.get(
        "/api/orders",
        params={
            "scope": "all",
            "detail_level": "full",
            "page": 1,
            "page_size": page_size,
        },
    )


@pytest.mark.parametrize("role", ["admin", "workshop"])
def test_full_list_page_context_is_byte_equivalent_to_single_order_path(
    full_list_app,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    app, _engine, current_user = full_list_app
    current_user["value"] = _user(role)
    original = orders_api._order_response

    with TestClient(app) as client:
        optimized = _request(client, page_size=20)

        def legacy_order_response(order, user, **kwargs):
            for key in PAGE_CONTEXT_KEYS:
                kwargs.pop(key, None)
            return original(order, user, **kwargs)

        monkeypatch.setattr(orders_api, "_order_response", legacy_order_response)
        legacy = _request(client, page_size=20)

    assert optimized.status_code == legacy.status_code == 200
    assert optimized.content == legacy.content
    rows = optimized.json()["items"]
    assert rows and rows[0]["items"]
    if role == "workshop":
        assert all("total_amount" not in row for row in rows)
        assert all("payment_status" not in row for row in rows)
        assert all(
            field not in item
            for row in rows
            for item in row["items"]
            for field in ("unit_price", "subtotal", "sale_amount")
        )
    else:
        assert all("total_amount" in row for row in rows)
        assert all(
            "sale_amount" in item and "estimated_cost" in item
            for row in rows
            for item in row["items"]
        )


def test_full_list_select_count_is_bounded_by_page_queries(full_list_app) -> None:
    app, engine, current_user = full_list_app
    current_user["value"] = _user("admin")
    counts: dict[int, int] = {}
    with TestClient(app) as client:
        for page_size in (5, 25):
            selects = 0

            def count_sql(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _many,
            ):
                nonlocal selects
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    selects += 1

            event.listen(engine, "before_cursor_execute", count_sql)
            try:
                response = _request(client, page_size=page_size)
            finally:
                event.remove(engine, "before_cursor_execute", count_sql)
            assert response.status_code == 200, response.text
            counts[page_size] = selects

    assert counts[25] <= 45
    assert counts[25] <= counts[5] + 2

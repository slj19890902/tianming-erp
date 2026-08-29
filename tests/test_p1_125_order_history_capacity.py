from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.api import orders as orders_api
from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.orders import router as orders_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.core.time_contract import beijing_now_naive
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import (
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User


PASSWORD = "123456"


@pytest.fixture()
def capacity_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-125-order-capacity.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = beijing_now_naive().replace(microsecond=0)
    with factory() as db:
        admin = User(
            username="p1125-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="P1-125 Admin",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(
            customer_number=125,
            customer_code="P1125",
            name="长期容量测试客户",
        )
        db.add_all([admin, customer])
        db.flush()

        def add_order(
            *,
            key: str,
            code: str,
            activity_at: datetime,
            status: str = "pending_delivery",
        ) -> tuple[Order, OrderItem]:
            product = Product(
                customer_id=customer.id,
                product_code=code,
                customer_material_code=f"MAT-{code}",
                product_name=f"容量纸箱-{key}",
                box_style="普通箱",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"P1125-{key}",
                customer_id=customer.id,
                customer_po=f"PO-{key}",
                order_date=activity_at.date(),
                delivery_date=activity_at.date(),
                status=status,
                payment_status="unpaid",
                total_amount=Decimal("10"),
                created_at=activity_at,
                updated_at=activity_at,
            )
            db.add(order)
            db.flush()
            item = OrderItem(
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
                snapshot_product_name=product.product_name,
            )
            db.add(item)
            db.flush()
            return order, item

        def complete_order(
            *,
            key: str,
            code: str,
            activity_at: datetime,
        ) -> tuple[Order, OrderItem]:
            order, item = add_order(key=key, code=code, activity_at=activity_at)
            delivery = Delivery(
                delivery_number=f"DN-{key}",
                customer_id=customer.id,
                delivery_date=activity_at.date(),
                status="dispatched",
                total_quantity=10,
            )
            db.add(delivery)
            db.flush()
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=item.id,
                delivered_quantity=10,
            )
            db.add(delivery_item)
            db.flush()
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=activity_at.date(),
                status="confirmed",
            )
            db.add(receipt)
            db.flush()
            receipt_item = ReturnReceiptItem(
                return_receipt_id=receipt.id,
                delivery_item_id=delivery_item.id,
                actual_received_quantity=10,
            )
            db.add(receipt_item)
            db.flush()
            statement = Statement(
                statement_number=f"ST-{key}",
                customer_id=customer.id,
                statement_month=activity_at.strftime("%Y-%m"),
                total_receivable=Decimal("10"),
                total_gross_profit=Decimal("0"),
                invoiced_amount=Decimal("0"),
                settled_amount=Decimal("0"),
                status="unsettled",
                created_at=activity_at,
            )
            db.add(statement)
            db.flush()
            db.add(
                StatementItem(
                    statement_id=statement.id,
                    return_receipt_item_id=receipt_item.id,
                    actual_received_quantity=10,
                    unit_price_snapshot=Decimal("1"),
                    unit_cost_snapshot=Decimal("0"),
                    receivable_amount=Decimal("10"),
                    gross_profit_amount=Decimal("10"),
                )
            )
            db.add(
                Invoice(
                    statement_id=statement.id,
                    invoice_number=f"INV-{key}",
                    invoice_date=activity_at.date(),
                    invoice_amount=Decimal("10"),
                    created_at=activity_at,
                )
            )
            db.add(
                SettlementRecord(
                    statement_id=statement.id,
                    settled_amount=Decimal("10"),
                    settlement_date=activity_at.date(),
                    account="P1-125",
                    created_at=activity_at,
                )
            )
            item.delivered_quantity = 10
            order.status = "delivered"
            order.updated_at = activity_at
            db.flush()
            return order, item

        active_ids = []
        for index in range(30):
            order, _item = add_order(
                key=f"ACTIVE-{index:02d}",
                code=f"ACTIVE-{index:02d}",
                activity_at=now - timedelta(days=45, minutes=index),
            )
            active_ids.append(order.id)
        recent_completed, _ = complete_order(
            key="RECENT-COMPLETED",
            code="RECENT-EXACT-001",
            activity_at=now - timedelta(days=2),
        )
        old_completed, _ = complete_order(
            key="OLD-COMPLETED",
            code="ARCHIVE-EXACT-001",
            activity_at=now - timedelta(days=40),
        )
        partial_code, _ = complete_order(
            key="OLD-PARTIAL-CODE",
            code="ARCHIVE-EXACT-001-SUFFIX",
            activity_at=now - timedelta(days=50),
        )
        # Deliberately make order-row timestamps disagree with finance facts.
        # Recent completion must follow invoice/settlement activity, not an
        # unrelated edit of the order row.
        db.flush()
        db.execute(
            update(Order)
            .where(Order.id == recent_completed.id)
            .values(updated_at=now - timedelta(days=40))
        )
        db.execute(
            update(Order)
            .where(Order.id == old_completed.id)
            .values(updated_at=now - timedelta(days=1))
        )
        db.execute(
            update(Order)
            .where(Order.id == partial_code.id)
            .values(updated_at=now - timedelta(days=50))
        )
        db.commit()
        ids = {
            "active": active_ids,
            "recent_completed": recent_completed.id,
            "old_completed": old_completed.id,
            "partial_code": partial_code.id,
        }

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


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "p1125-admin", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def test_order_default_page_projects_only_the_requested_page(
    capacity_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, ids = capacity_app
    original = orders_api.build_order_business_statuses
    projection_batch_sizes: list[int] = []

    def measured_projection(db, orders, **kwargs):
        projection_batch_sizes.append(len(orders))
        return original(db, orders, **kwargs)

    monkeypatch.setattr(orders_api, "build_order_business_statuses", measured_projection)
    with TestClient(app) as client:
        _login(client)
        response = client.get(
            "/api/orders",
            params={
                "scope": "active",
                "page": 1,
                "page_size": 2,
                "detail_level": "summary",
                "include_unfinished_total": False,
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == len(ids["active"])
    assert len(payload["items"]) == 2
    assert payload["unfinished_total"] is None
    assert projection_batch_sizes
    assert max(projection_batch_sizes) <= 2


def test_recent_completed_and_conditioned_history_are_separate(capacity_app) -> None:
    app, ids = capacity_app
    with TestClient(app) as client:
        _login(client)
        recent = client.get(
            "/api/orders",
            params={
                "scope": "completed",
                "history_mode": "recent",
                "recent_days": 10,
                "include_unfinished_total": False,
                "detail_level": "summary",
            },
        )
        empty_history = client.get(
            "/api/orders",
            params={
                "scope": "completed",
                "history_mode": "history",
                "include_unfinished_total": False,
                "detail_level": "summary",
            },
        )
        empty_history_customer_options = client.get(
            "/api/orders/customer-options",
            params={"scope": "completed", "history_mode": "history"},
        )
        exact_history = client.get(
            "/api/orders",
            params={
                "scope": "completed",
                "history_mode": "history",
                "product_code": "ARCHIVE-EXACT-001",
                "product_code_match": "exact",
                "include_unfinished_total": False,
                "detail_level": "summary",
            },
        )

    assert recent.status_code == 200, recent.text
    assert [row["id"] for row in recent.json()["items"]] == [
        ids["recent_completed"]
    ]
    assert empty_history.status_code == 200, empty_history.text
    assert empty_history.json()["items"] == []
    assert empty_history.json()["history_search_required"] is True
    assert empty_history_customer_options.status_code == 200
    assert empty_history_customer_options.json()["items"] == []
    assert empty_history_customer_options.json()["history_search_required"] is True
    assert exact_history.status_code == 200, exact_history.text
    assert [row["id"] for row in exact_history.json()["items"]] == [
        ids["old_completed"]
    ]
    assert ids["partial_code"] not in {
        row["id"] for row in exact_history.json()["items"]
    }


def test_old_unfinished_orders_are_never_hidden_by_recent_window(capacity_app) -> None:
    app, ids = capacity_app
    with TestClient(app) as client:
        _login(client)
        response = client.get(
            "/api/orders",
            params={
                "scope": "active",
                "history_mode": "recent",
                "recent_days": 10,
                "page": 1,
                "page_size": 50,
                "include_unfinished_total": False,
                "detail_level": "summary",
            },
        )

    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["items"]} == set(ids["active"])

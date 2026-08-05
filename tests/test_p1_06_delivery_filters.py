from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def delivery_filter_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deliveries import router as deliveries_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "delivery_filters.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        admin = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            display_name="管理员",
            must_change_password=False,
        )
        scoped = User(
            username="scoped",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="范围用户",
            display_name="范围用户",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customers = [
            Customer(customer_number=1, customer_code="THCJ", name="天华超净", payment_term_days=30, credit_limit=Decimal("0")),
            Customer(customer_number=2, customer_code="MJ", name="明俊德", payment_term_days=30, credit_limit=Decimal("0")),
        ]
        session.add_all([admin, scoped, *customers])
        session.flush()
        session.add_all(
            [
                UserPermissionOverride(user_id=scoped.id, permission_code="deliveries.view", is_allowed=True),
                UserCustomerScope(user_id=scoped.id, customer_id=customers[0].id),
            ]
        )
        products = [
            Product(customer_id=customers[0].id, product_code="TH-22000008", customer_material_code="TH-22000008", product_name="天华外箱", box_category="normal"),
            Product(customer_id=customers[1].id, product_code="MJ-001", customer_material_code="MJ-001", product_name="明俊德内盒", box_category="normal"),
        ]
        session.add_all(products)
        session.flush()
        orders = [
            Order(order_number="SO-TH-001", customer_id=customers[0].id, customer_po="TH-PO-77", order_date=date(2026, 7, 1), delivery_date=date(2026, 7, 2), status="pending_delivery", payment_status="unpaid", total_amount=Decimal("10")),
            Order(order_number="SO-MJ-001", customer_id=customers[1].id, customer_po="MJ-PO-88", order_date=date(2026, 7, 1), delivery_date=date(2026, 7, 3), status="pending_delivery", payment_status="unpaid", total_amount=Decimal("20")),
        ]
        session.add_all(orders)
        session.flush()
        items = [
            OrderItem(order_id=orders[0].id, product_id=products[0].id, quantity=10, unit_price=Decimal("1"), subtotal=Decimal("10"), material_status="received", snapshot_product_name="天华外箱", snapshot_spec="400×300×200mm"),
            OrderItem(order_id=orders[1].id, product_id=products[1].id, quantity=20, unit_price=Decimal("1"), subtotal=Decimal("20"), material_status="received", snapshot_product_name="明俊德内盒", snapshot_spec="500×400×300mm"),
        ]
        session.add_all(items)
        session.flush()
        base = datetime(2026, 7, 10, 9, 0, 0)
        deliveries = [
            Delivery(delivery_number="TH-0001", customer_id=customers[0].id, delivery_date=date(2026, 7, 10), status="dispatched", total_quantity=10, created_at=base),
            Delivery(delivery_number="TH-0002", customer_id=customers[0].id, delivery_date=date(2026, 7, 11), status="pending", total_quantity=10, created_at=base + timedelta(minutes=1)),
            Delivery(delivery_number="MJ-0001", customer_id=customers[1].id, delivery_date=date(2026, 7, 12), status="dispatched", total_quantity=20, created_at=base + timedelta(minutes=2)),
            Delivery(delivery_number="STOCK-0003", customer_id=customers[0].id, delivery_date=date(2026, 7, 13), status="voided", source_mode="unordered_finished", total_quantity=3, created_at=base + timedelta(minutes=3)),
        ]
        session.add_all(deliveries)
        session.flush()
        session.add_all(
            [
                DeliveryItem(delivery_id=deliveries[0].id, order_item_id=items[0].id, delivered_quantity=10),
                DeliveryItem(delivery_id=deliveries[1].id, order_item_id=items[0].id, delivered_quantity=10),
                DeliveryItem(delivery_id=deliveries[2].id, order_item_id=items[1].id, delivered_quantity=20),
                DeliveryItem(
                    delivery_id=deliveries[3].id,
                    source_type="unordered_finished",
                    product_id=products[0].id,
                    product_code_snapshot="TH-STOCK-X",
                    product_name_snapshot="库存专用外箱",
                    specification_snapshot="610×410×310mm",
                    unit_snapshot="只",
                    unit_price_snapshot=Decimal("1"),
                    price_source="product_default",
                    delivered_quantity=3,
                ),
                ReturnReceipt(delivery_id=deliveries[0].id, actual_received_date=date(2026, 7, 11), status="confirmed"),
                ReturnReceipt(delivery_id=deliveries[2].id, actual_received_date=date(2026, 7, 13), status="cancelled"),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")
    app.include_router(deliveries_router, prefix="/api/deliveries")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "RolePass123!"})
    assert response.status_code == 200, response.text


def _numbers(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["delivery_number"] for row in response.json()["items"]]


def test_delivery_filters_compose_and_keep_stable_pagination(delivery_filter_app) -> None:
    with TestClient(delivery_filter_app) as client:
        _login(client, "admin")
        response = client.get(
            "/api/deliveries",
            params=[
                ("customer_ids", 1),
                ("statuses", "dispatched"),
                ("return_status", "confirmed"),
                ("delivery_no", "th"),
                ("order_no", "so-th"),
                ("customer_po", "po-77"),
                ("product_code", "22000008"),
                ("product_name", "外箱"),
                ("spec", "400×300"),
                ("date_from", "2026-07-10"),
                ("date_to", "2026-07-10"),
            ],
        )
        assert _numbers(response) == ["TH-0001"]
        assert response.json()["total"] == 1

        first = client.get("/api/deliveries", params={"customer_ids": 1, "page_size": 1, "page": 1})
        second = client.get("/api/deliveries", params={"customer_ids": 1, "page_size": 1, "page": 2})
        assert _numbers(first) == ["TH-0002"]
        assert _numbers(second) == ["TH-0001"]
        assert first.json()["total"] == second.json()["total"] == 2


def test_return_statuses_and_existing_status_parameter_remain_compatible(delivery_filter_app) -> None:
    with TestClient(delivery_filter_app) as client:
        _login(client, "admin")
        assert _numbers(client.get("/api/deliveries", params={"return_status": "waiting_receipt"})) == ["MJ-0001", "TH-0002"]
        assert _numbers(client.get("/api/deliveries", params={"status": "dispatched", "return_status": "waiting_receipt"})) == ["MJ-0001"]
        assert _numbers(client.get("/api/deliveries", params=[("return_status", "confirmed"), ("return_status", "cancelled")])) == ["MJ-0001", "TH-0001"]
        assert _numbers(client.get("/api/deliveries", params={"status": "pending"})) == ["TH-0002"]


def test_dashboard_pending_receipt_matches_cancelled_receipt_drilldown(
    delivery_filter_app,
) -> None:
    with TestClient(delivery_filter_app) as client:
        _login(client, "admin")
        overview = client.get("/api/dashboard/overview")
        assert overview.status_code == 200, overview.text
        cards = {row["key"]: row for row in overview.json()["cards"]}
        assert cards["pending_receipt"]["count"] == 1
        drilldown = client.get(
            "/api/deliveries",
            params={"status": "dispatched", "return_status": "waiting_receipt"},
        )
        assert _numbers(drilldown) == ["MJ-0001"]


def test_customer_scope_applies_before_filters_and_blocks_direct_out_of_scope_request(delivery_filter_app) -> None:
    with TestClient(delivery_filter_app) as client:
        _login(client, "scoped")
        assert _numbers(client.get("/api/deliveries", params={"product_name": "内盒"})) == []
        assert _numbers(client.get("/api/deliveries", params={"customer_ids": 1})) == ["TH-0002", "TH-0001"]
        forbidden = client.get("/api/deliveries", params={"customer_ids": 2})
        assert forbidden.status_code == 403


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("THCJ", ["TH-0002", "TH-0001"]),
        ("天华超净", ["TH-0002", "TH-0001"]),
        ("TH-0001", ["TH-0001"]),
        ("SO-TH-001", ["TH-0002", "TH-0001"]),
        ("TH-PO-77", ["TH-0002", "TH-0001"]),
        ("TH-22000008", ["TH-0002", "TH-0001"]),
        ("天华外箱", ["TH-0002", "TH-0001"]),
        ("400×300×200", ["TH-0002", "TH-0001"]),
    ],
)
def test_unified_keyword_searches_all_delivery_identity_fields(
    delivery_filter_app,
    keyword: str,
    expected: list[str],
) -> None:
    with TestClient(delivery_filter_app) as client:
        _login(client, "admin")
        assert _numbers(
            client.get("/api/deliveries", params={"keyword": keyword})
        ) == expected


def test_unified_keyword_includes_unordered_snapshots_and_keeps_scope(
    delivery_filter_app,
) -> None:
    with TestClient(delivery_filter_app) as client:
        _login(client, "admin")
        for keyword in ("TH-STOCK-X", "库存专用外箱", "610×410"):
            assert _numbers(
                client.get(
                    "/api/deliveries",
                    params={"keyword": keyword, "status": "voided"},
                )
            ) == ["STOCK-0003"]

    with TestClient(delivery_filter_app) as client:
        _login(client, "scoped")
        assert _numbers(
            client.get("/api/deliveries", params={"keyword": "明俊德"})
        ) == []
        assert _numbers(
            client.get("/api/deliveries", params={"keyword": "THCJ"})
        ) == ["TH-0002", "TH-0001"]


def test_unified_keyword_composes_with_status_date_receipt_and_pagination(
    delivery_filter_app,
) -> None:
    with TestClient(delivery_filter_app) as client:
        _login(client, "admin")
        filtered = client.get(
            "/api/deliveries",
            params={
                "keyword": "THCJ",
                "status": "dispatched",
                "return_status": "confirmed",
                "date_from": "2026-07-10",
                "date_to": "2026-07-10",
            },
        )
        assert _numbers(filtered) == ["TH-0001"]

        first = client.get(
            "/api/deliveries",
            params={"keyword": "THCJ", "page": 1, "page_size": 1},
        )
        second = client.get(
            "/api/deliveries",
            params={"keyword": "THCJ", "page": 2, "page_size": 1},
        )
        assert _numbers(first) == ["TH-0002"]
        assert _numbers(second) == ["TH-0001"]
        assert first.json()["total"] == second.json()["total"] == 2

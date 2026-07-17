from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def incoming_time_api_app(tmp_path):
    """A fully isolated SQLite app for incoming timestamp JSON contracts."""
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n033_incoming_time.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    with session_factory() as session:
        admin = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="admin",
            display_name="admin",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="N033",
            name="N033 时间契约客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([admin, customer])
        session.flush()

        product = Product(
            customer_id=customer.id,
            product_code="N033-001",
            customer_material_code="N033-MATERIAL",
            product_name="N033 时间契约纸箱",
            legacy_material_text="K=A-BC",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
            box_category="normal",
        )
        session.add(product)
        session.flush()

        pending_order = Order(
            order_number="N033-PENDING",
            customer_id=customer.id,
            order_date=date(2026, 7, 17),
            delivery_date=date(2026, 7, 18),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("100"),
            created_at=datetime(2026, 7, 17, 0, 1, 2),
        )
        history_order = Order(
            order_number="N033-HISTORY",
            customer_id=customer.id,
            order_date=date(2026, 7, 17),
            delivery_date=date(2026, 7, 18),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("100"),
            created_at=datetime(2026, 7, 17, 0, 1, 2),
        )
        session.add_all([pending_order, history_order])
        session.flush()

        session.add_all(
            [
                OrderItem(
                    order_id=pending_order.id,
                    product_id=product.id,
                    quantity=10,
                    unit_price=Decimal("10"),
                    subtotal=Decimal("100"),
                    material_status="pending",
                    requisition_status="供应商已排单",
                    supplier_delivery_time=datetime(2026, 7, 17, 8, 30),
                    snapshot_product_name="N033 时间契约纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
                OrderItem(
                    order_id=history_order.id,
                    product_id=product.id,
                    quantity=10,
                    unit_price=Decimal("10"),
                    subtotal=Decimal("100"),
                    material_status="received",
                    material_received_at=datetime(2026, 7, 17, 1, 2, 3),
                    material_received_by=admin.id,
                    snapshot_product_name="N033 时间契约纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(incoming_router, prefix="/api/incoming")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def test_pending_api_serializes_utc_and_beijing_datetimes_and_nulls(
    incoming_time_api_app,
) -> None:
    with TestClient(incoming_time_api_app) as client:
        _login(client)
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["order_number"] == "N033-PENDING"
    assert item["created_at"] == "2026-07-17T00:01:02Z"
    assert item["supplier_delivery_time"] == "2026-07-17T08:30:00+08:00"
    assert item["material_received_at"] is None


def test_history_api_serializes_received_at_as_utc_and_preserves_nulls(
    incoming_time_api_app,
) -> None:
    with TestClient(incoming_time_api_app) as client:
        _login(client)
        response = client.get("/api/incoming/history")

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["order_number"] == "N033-HISTORY"
    assert item["material_received_at"] == "2026-07-17T01:02:03Z"
    assert item["supplier_delivery_time"] is None

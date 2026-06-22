from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


def test_dashboard_frontend_loads_real_kpi_endpoint() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'axios.get("/api/dashboard/kpi")' in source
    assert "monthly_gross_profit" in source
    assert "含模拟订单" not in source


def test_dashboard_kpi_uses_real_database_aggregates(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import (
        ReturnReceipt,
        ReturnReceiptItem,
        Statement,
        StatementItem,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    today = date.today()
    with factory() as session:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        session.add_all([user, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P1",
            customer_material_code="P1",
            product_name="测试纸箱",
            box_category="normal",
            cost_unit_price=Decimal("2.70"),
        )
        session.add(product)
        session.flush()
        received_order = Order(
            order_number="PO-KPI-001",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="partially_delivered",
            payment_status="unpaid",
            total_amount=0,
        )
        material_order = Order(
            order_number="PO-KPI-002",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="pending_production",
            payment_status="unpaid",
            total_amount=0,
        )
        session.add_all([received_order, material_order])
        session.flush()
        received_item = OrderItem(
            order_id=received_order.id,
            product_id=product.id,
            quantity=100,
            delivered_quantity=80,
            unit_price=Decimal("3.60"),
            subtotal=0,
            material_status="received",
            snapshot_product_name="测试纸箱",
        )
        pending_material_item = OrderItem(
            order_id=material_order.id,
            product_id=product.id,
            quantity=50,
            delivered_quantity=0,
            unit_price=Decimal("2.00"),
            subtotal=0,
            material_status="pending",
            snapshot_product_name="待收料纸箱",
        )
        session.add_all([received_item, pending_material_item])
        session.flush()
        delivery = Delivery(
            delivery_number="DH-KPI-001",
            customer_id=customer.id,
            delivery_date=today,
            status="dispatched",
            total_quantity=80,
        )
        session.add(delivery)
        session.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=received_item.id,
            delivered_quantity=80,
        )
        session.add(delivery_item)
        session.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=today,
            signed_by="王经理",
            status="confirmed",
        )
        session.add(receipt)
        session.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=78,
            difference_reason="拒收2个",
        )
        session.add(receipt_item)
        session.flush()
        statement = Statement(
            statement_number="ST-KPI-001",
            customer_id=customer.id,
            statement_month=today.strftime("%Y-%m"),
            total_receivable=Decimal("280.80"),
            total_gross_profit=Decimal("70.20"),
            status="unsettled",
        )
        session.add(statement)
        session.flush()
        session.add(
            StatementItem(
                statement_id=statement.id,
                return_receipt_item_id=receipt_item.id,
                actual_received_quantity=78,
                unit_price_snapshot=Decimal("3.60"),
                unit_cost_snapshot=Decimal("2.70"),
                receivable_amount=Decimal("280.80"),
                gross_profit_amount=Decimal("70.20"),
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "RolePass123!"},
        ).status_code == 200
        response = client.get("/api/dashboard/kpi")

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["monthly_revenue"])) == Decimal("280.80")
    assert Decimal(str(body["monthly_gross_profit"])) == Decimal("70.20")
    assert Decimal(str(body["outstanding_receivables"])) == Decimal("280.80")
    assert body["today_pending_delivery_tasks"] == 1
    assert body["today_pending_incoming_tasks"] == 1

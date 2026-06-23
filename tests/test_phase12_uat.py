from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def phase12_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.customers import router as customers_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.api.incoming import router as incoming_router
    from app.api.materials import router as materials_router
    from app.api.products import router as products_router
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
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    upload_dir = tmp_path / "uploads"
    monkeypatch.setenv("ERP_DRAWING_DIR", str(upload_dir))
    engine = create_sqlite_engine(tmp_path / "phase12.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "finance", "sales", "workshop")
        ]
        active = Customer(
            customer_number=1,
            customer_code="ACTIVE",
            name="苏州正常客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="配送",
        )
        inactive = Customer(
            customer_number=2,
            customer_code="STOP",
            name="昆山停用客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="物流",
            status="inactive",
            is_active=False,
        )
        material = Material(
            code="WCX1",
            layer_count=5,
            flute_type="AB",
            basis_weight_description="供应商A 170g/130g/80g/170g/150g 高强",
        )
        session.add_all([*users, active, inactive, material])
        session.flush()
        product = Product(
            customer_id=active.id,
            product_code="21301028",
            customer_material_code="21301028",
            product_name="中性外箱",
            material_id=material.id,
            box_category="normal",
            cost_unit_price=Decimal("2.00"),
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260614-001",
            customer_id=active.id,
            order_date=date(2026, 6, 14),
            delivery_date=date(2026, 6, 21),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("300"),
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            delivered_quantity=80,
            unit_price=Decimal("3.00"),
            subtotal=Decimal("300"),
            material_status="pending",
            requisition_status="已报料",
            snapshot_product_code="21301028",
            snapshot_product_name="中性外箱",
            snapshot_material="WCX1",
        )
        session.add(item)
        session.flush()
        delivery = Delivery(
            delivery_number="DH-20260614-001",
            customer_id=active.id,
            delivery_date=date(2026, 6, 14),
            status="dispatched",
            total_quantity=80,
            dispatched_at=datetime(2026, 6, 14, 9, 0),
        )
        session.add(delivery)
        session.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item.id,
            delivered_quantity=80,
        )
        session.add(delivery_item)
        session.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 6, 14),
            signed_by="王经理",
            status="confirmed",
        )
        session.add(receipt)
        session.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=78,
            difference_reason="压坏2个",
        )
        session.add(receipt_item)
        session.flush()
        statement = Statement(
            statement_number="ST-202606-001",
            customer_id=active.id,
            statement_month="2026-06",
            total_receivable=Decimal("234"),
            total_gross_profit=Decimal("78"),
            status="unsettled",
        )
        session.add(statement)
        session.flush()
        session.add(
            StatementItem(
                statement_id=statement.id,
                return_receipt_item_id=receipt_item.id,
                actual_received_quantity=78,
                unit_price_snapshot=Decimal("3.00"),
                unit_cost_snapshot=Decimal("2.00"),
                receivable_amount=Decimal("234"),
                gross_profit_amount=Decimal("78"),
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(customers_router, prefix="/api/master/customers")
    app.include_router(materials_router, prefix="/api/master/materials")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(incoming_router, prefix="/api/incoming")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory, upload_dir


def _login(client: TestClient, role: str = "admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def test_customer_list_hides_inactive_and_delete_blocks_open_order(phase12_app):
    app, session_factory, _ = phase12_app
    with TestClient(app) as client:
        _login(client)
        visible = client.get("/api/master/customers")
        all_rows = client.get(
            "/api/master/customers",
            params={"include_inactive": True},
        )
        blocked = client.delete("/api/master/customers/1")
        removed = client.delete("/api/master/customers/2")

    assert visible.json()["total"] == 1
    assert all_rows.json()["total"] == 2
    assert blocked.status_code == 400
    assert removed.status_code == 204
    with session_factory() as session:
        customer = session.get(__import__("app.models.customer", fromlist=["Customer"]).Customer, 2)
        assert customer is not None
        assert customer.is_active is False


def test_admin_can_reenable_inactive_customer(phase12_app):
    from app.models.audit import OperationLog
    from app.models.customer import Customer

    app, session_factory, _ = phase12_app
    with TestClient(app) as client:
        _login(client)
        enabled = client.put(
            "/api/master/customers/2/status",
            json={"is_active": True},
        )
        visible = client.get("/api/master/customers")

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["is_active"] is True
    assert enabled.json()["status"] == "active"
    assert visible.json()["total"] == 2
    with session_factory() as session:
        assert session.get(Customer, 2).is_active is True
        actions = list(
            session.scalars(
                select(OperationLog.action).where(
                    OperationLog.resource == "Customer"
                )
            )
        )
    assert "ENABLE" in actions


def test_material_normalizes_weight_and_rejects_unknown_flute(phase12_app):
    app, _, _ = phase12_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/master/materials",
            json={
                "code": "TEST-AB",
                "layer_count": 5,
                "flute_type": "AB",
                "basis_weight_description": "嘉林亿170克/130g/80克/170g/150g",
            },
        )
        invalid = client.post(
            "/api/master/materials",
            json={"code": "BAD", "flute_type": "BC"},
        )

    assert created.status_code == 201, created.text
    assert created.json()["basis_weight_description"] == "170g/130g/80g/170g/150g"
    assert invalid.status_code == 422


def test_product_drawing_upload_saves_compressed_files_not_base64(phase12_app):
    app, _, upload_dir = phase12_app
    image = Image.new("RGB", (2400, 1600), "white")
    source = BytesIO()
    image.save(source, format="PNG")
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.post(
            "/api/master/products/1/drawing",
            files={"file": ("drawing.png", source.getvalue(), "image/png")},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["drawing_path"].startswith("/static/uploads/drawings/")
    assert data["thumbnail_path"].startswith("/static/uploads/drawings/")
    files = list(upload_dir.glob("*"))
    assert len(files) == 2
    assert all(path.stat().st_size < len(source.getvalue()) for path in files)
    assert "base64" not in str(data).lower()


def test_wms_receive_synchronizes_requisition_status(phase12_app):
    from app.models.order import OrderItem

    app, session_factory, _ = phase12_app
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.put("/api/incoming/receive/1")

    assert response.status_code == 200
    assert response.json()["material_status"] == "received"
    assert response.json()["requisition_status"] == "已入库"
    with session_factory() as session:
        assert session.get(OrderItem, 1).requisition_status == "已入库"


def test_statement_excel_export_is_valid_workbook(phase12_app):
    app, _, _ = phase12_app
    with TestClient(app) as client:
        _login(client, "finance")
        response = client.get("/api/finance/statements/1/export")

    assert response.status_code == 200, response.text
    assert "spreadsheetml" in response.headers["content-type"]
    workbook = load_workbook(BytesIO(response.content))
    sheet = workbook.active
    assert sheet["A1"].value == "月结对账单"
    assert "苏州正常客户" in sheet["A2"].value
    assert sheet.max_row >= 7

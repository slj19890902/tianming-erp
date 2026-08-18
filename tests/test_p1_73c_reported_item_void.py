from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def reported_item_void_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-73c.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="P1-73C Admin",
            display_name="P1-73C Admin",
            must_change_password=False,
        )
        viewer = User(
            username="viewer",
            password_hash=hash_password("ViewerPass123!"),
            role="sales",
            real_name="P1-73C Viewer",
            display_name="P1-73C Viewer",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(
            customer_number=7301,
            customer_code="P173-C",
            name="P1-73C 客户",
            chinese_short_name="七三客户",
        )
        db.add_all([admin, viewer, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P173-C-BOX",
            customer_material_code="P173-C-BOX",
            product_name="P1-73C 三层外箱",
            box_category="normal",
        )
        db.add(product)
        db.flush()
        sales_order = Order(
            order_number="TM-P173-C-001",
            customer_id=customer.id,
            order_date=date(2026, 8, 18),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("30"),
        )
        db.add(sales_order)
        db.flush()
        order_item = OrderItem(
            order_id=sales_order.id,
            product_id=product.id,
            item_order_number="TM-P173-C-001-01",
            item_sequence=1,
            quantity=30,
            unit_price=Decimal("1"),
            subtotal=Decimal("30"),
            material_status="pending",
            requisition_status="已报料",
            requisition_qty=30,
            supplier_order_number="SRO-P173-C",
            snapshot_product_code=product.product_code,
            snapshot_product_name=product.product_name,
        )
        db.add(order_item)
        db.flush()
        supplier_order = SupplierRequisitionOrder(
            order_number="SRO-P173-C",
            supplier_name="P1-73C 纸板厂",
            total_quantity=30,
            requisition_qty=30,
            stock_deduction_qty=0,
            status="confirmed",
            created_by=admin.id,
            created_at=datetime(2026, 8, 18, 9, 0, 0),
        )
        db.add(supplier_order)
        db.flush()
        rows = []
        for index in range(1, 4):
            row = SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=order_item.id,
                source_key=f"order_item:{order_item.id}",
                product_id=product.id,
                order_number=sales_order.order_number,
                product_code=f"P173-C-{index}",
                product_name=f"P1-73C 外箱 {index}",
                customer_name=customer.name,
                report_length_mm=600,
                report_width_mm=400,
                quantity=10,
                requisition_qty=10,
                stock_deduction_qty=0,
            )
            db.add(row)
            rows.append(row)
        db.commit()
        ids = {
            "user_id": admin.id,
            "customer_id": customer.id,
            "order_id": sales_order.id,
            "order_item_id": order_item.id,
            "supplier_order_id": supplier_order.id,
            "item_ids": [row.id for row in rows],
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "AdminPass123!"},
    )
    assert response.status_code == 200, response.text


def _login_viewer(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "viewer", "password": "ViewerPass123!"},
    )
    assert response.status_code == 200, response.text


def _void(client: TestClient, item_id: int, key: str, version: int = 1):
    return client.put(
        f"/api/requisition/supplier-order-items/{item_id}/void",
        json={
            "expected_version": version,
            "idempotency_key": key,
            "confirmed": True,
        },
    )


def test_void_middle_line_is_idempotent_and_recomputes_exact_coverage(
    reported_item_void_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    app, factory, ids = reported_item_void_app
    middle = ids["item_ids"][1]
    with TestClient(app) as client:
        _login(client)
        first = _void(client, middle, "p1-73c-middle")
        replay = _void(client, middle, "p1-73c-middle")
        stale = _void(client, middle, "p1-73c-other")
        listing = client.get(
            "/api/requisition/reported-items",
            params={"source_type": "supplier_order", "page_size": 20},
        )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert stale.status_code == 409
    assert first.json()["item"]["status"] == "voided"
    assert first.json()["item"]["version"] == 2
    assert first.json()["supplier_order"]["status"] == "confirmed"
    assert first.json()["supplier_order"]["requisition_qty"] == 20
    assert first.json()["order_item"]["requisition_qty"] == 20
    listed = {row["item_id"]: row for row in listing.json()["items"]}
    assert listed[middle]["status"] == "voided"
    assert listed[middle]["can_void_item"] is False
    assert listed[ids["item_ids"][0]]["can_void_item"] is True
    with factory() as db:
        assert db.scalar(select(func.count(SupplierRequisitionOrderItem.id))) == 3
        assert db.get(OrderItem, ids["order_item_id"]).requisition_qty == 20
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code
                == "requisition.supplier_order_item.void"
            )
        ) == 1


def test_voiding_last_active_line_voids_header_and_restores_pending_state(
    reported_item_void_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, factory, ids = reported_item_void_app
    with TestClient(app) as client:
        _login(client)
        responses = [
            _void(client, item_id, f"p1-73c-last-{item_id}")
            for item_id in ids["item_ids"]
        ]
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[-1].json()["supplier_order"]["status"] == "voided"
    with factory() as db:
        header = db.get(SupplierRequisitionOrder, ids["supplier_order_id"])
        order_item = db.get(OrderItem, ids["order_item_id"])
        assert header.status == "voided"
        assert header.requisition_qty == 0
        assert order_item.requisition_status == "未报料"
        assert order_item.requisition_qty is None
        assert order_item.supplier_order_number is None


def test_posted_receipt_blocks_item_void_without_partial_write(
    reported_item_void_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    app, factory, ids = reported_item_void_app
    target = ids["item_ids"][0]
    with factory() as db:
        receipt = IncomingReceipt(
            receipt_number="IR-P173-C",
            status="posted",
            received_at=datetime(2026, 8, 18, 10, 0, 0),
            received_by=ids["user_id"],
            idempotency_key="ir-p173-c",
        )
        db.add(receipt)
        db.flush()
        db.add(
            IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=ids["order_id"],
                order_item_id=ids["order_item_id"],
                supplier_order_id=ids["supplier_order_id"],
                supplier_order_item_id=target,
                planned_quantity=10,
                received_quantity=4,
                cumulative_received_quantity=4,
                variance_quantity=-6,
                variance_type="short",
                resolution_status="pending",
                resolution_action="await_supplier",
                status="posted",
            )
        )
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response = _void(client, target, "p1-73c-received")
    assert response.status_code == 409
    assert "来料实收" in str(response.json())
    with factory() as db:
        item = db.get(SupplierRequisitionOrderItem, target)
        assert (item.status, item.version, item.void_idempotency_key) == (
            "active",
            1,
            None,
        )


def test_void_requires_execute_permission_and_audit_failure_rolls_back(
    reported_item_void_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import requisition as requisition_api
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    app, factory, ids = reported_item_void_app
    target = ids["item_ids"][0]
    with TestClient(app) as client:
        _login_viewer(client)
        denied = _void(client, target, "p1-73c-denied")
    assert denied.status_code == 403

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("P1-73C audit failure")

    monkeypatch.setattr(requisition_api, "append_audit_event", fail_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        failed = _void(client, target, "p1-73c-audit-failure")
    assert failed.status_code == 500
    with factory() as db:
        item = db.get(SupplierRequisitionOrderItem, target)
        assert (item.status, item.version, item.void_idempotency_key) == (
            "active",
            1,
            None,
        )

from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


PASSWORD = "123456"
ROOT = Path(__file__).resolve().parents[1]
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")


@pytest.fixture()
def mobile_production_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.mobile_erp import router as mobile_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.core.time_contract import beijing_today, utc_now_naive
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.mold_tool import MoldTool
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "mobile-production.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = utc_now_naive()
    today = beijing_today()
    with factory() as db:
        workshop = User(
            username="mobile-workshop",
            password_hash=hash_password(PASSWORD),
            role="workshop",
            real_name="车间员工",
            must_change_password=False,
            customer_access_mode="selected",
        )
        sales = User(
            username="mobile-sales",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="销售员工",
            must_change_password=False,
        )
        visible = Customer(name="匿名车间客户", customer_code="MW")
        hidden = Customer(name="其他客户", customer_code="OTHER")
        db.add_all([workshop, sales, visible, hidden])
        db.flush()
        db.add(UserCustomerScope(user_id=workshop.id, customer_id=visible.id))
        mold = MoldTool(
            mold_code="MOBILE-MOLD-01",
            mold_name="匿名内盒模具",
            rack_location="M1-R02",
        )
        db.add(mold)
        db.flush()
        visible_product = Product(
            customer_id=visible.id,
            product_code="MOBILE-PROD-01",
            customer_material_code="MP01",
            product_name="匿名模切内盒",
            box_category="die_cut",
            box_style="模切内盒",
            production_process="模切后检查压线",
            mold_tool_id=mold.id,
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="HIDDEN-PROD-01",
            customer_material_code="HP01",
            product_name="不可见产品",
        )
        liner_product = Product(
            customer_id=visible.id,
            product_code="MOBILE-LINER-01",
            customer_material_code="ML01",
            product_name="匿名衬板",
            box_category="normal",
            box_style="衬板",
        )
        db.add_all([visible_product, hidden_product, liner_product])
        db.flush()

        def add_received(
            *,
            customer: Customer,
            product: Product,
            key: str,
            received_at,
            received: int,
            cumulative: int,
            planned: int,
        ) -> tuple[int, int]:
            order = Order(
                order_number=f"MOBILE-{key}",
                customer_id=customer.id,
                customer_po=f"PO-{key}",
                order_date=today,
                delivery_date=today + timedelta(days=2),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_sequence=1,
                item_order_number=f"MOBILE-{key}-001",
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="received",
                requisition_status="已入库",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="420×310×120",
                snapshot_material="SECRET-SUPPLIER-MATERIAL",
                snapshot_supplier_name="SECRET-SUPPLIER",
                snapshot_production_notes="印刷后模切",
                cardboard_len=Decimal("800"),
                cardboard_width=Decimal("600"),
                flute_type="B",
                snapshot_crease_type="净",
                snapshot_crease_left_mm=120,
                snapshot_crease_middle_mm=310,
                snapshot_crease_right_mm=120,
                snapshot_report_notes="长边顺瓦楞方向",
                drawing_file="private/order-drawing.pdf",
                supplier_order_number=f"SUP-{key}",
            )
            db.add(item)
            db.flush()
            task = ProductionTask(
                order_item_id=item.id,
                status="pending",
                planned_quantity=80,
                ordered_quantity_snapshot=100,
                material_received_quantity=cumulative,
                material_input_quantity=cumulative,
                output_factor=1,
                readiness_basis="material_received",
                version=1,
            )
            receipt = IncomingReceipt(
                receipt_number=f"REC-{key}",
                status="posted",
                received_at=received_at,
                received_by=workshop.id,
                idempotency_key=f"mobile-receipt-{key}",
            )
            db.add_all([task, receipt])
            db.flush()
            fact = IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=order.id,
                order_item_id=item.id,
                planned_quantity=planned,
                received_quantity=received,
                cumulative_received_quantity=cumulative,
                variance_quantity=cumulative - planned,
                variance_type="short" if cumulative < planned else "matched",
                resolution_status="pending" if cumulative < planned else "not_required",
                resolution_action="await_supplier" if cumulative < planned else None,
                status="posted",
            )
            db.add(fact)
            db.flush()
            return item.id, fact.id

        visible_item_id, visible_fact_id = add_received(
            customer=visible,
            product=visible_product,
            key="VISIBLE",
            received_at=now - timedelta(hours=2),
            received=60,
            cumulative=60,
            planned=100,
        )
        add_received(
            customer=hidden,
            product=hidden_product,
            key="HIDDEN",
            received_at=now - timedelta(hours=1),
            received=100,
            cumulative=100,
            planned=100,
        )
        add_received(
            customer=visible,
            product=visible_product,
            key="OLD",
            received_at=now - timedelta(days=10),
            received=100,
            cumulative=100,
            planned=100,
        )
        liner_item_id, _liner_fact_id = add_received(
            customer=visible,
            product=liner_product,
            key="LINER",
            received_at=now - timedelta(hours=1),
            received=100,
            cumulative=100,
            planned=100,
        )
        liner_task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == liner_item_id
            )
        )
        assert liner_task is not None
        liner_task.status = "completed"
        db.commit()
        ids = {"visible_item": visible_item_id, "visible_fact": visible_fact_id}

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(mobile_router, prefix="/api/mobile/erp")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def test_recent_production_is_formal_scoped_redacted_and_read_only(
    mobile_production_app,
) -> None:
    app, factory, ids = mobile_production_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.production import ProductionTask

    with factory() as db:
        before = (
            db.scalar(select(func.count(IncomingReceiptItem.id))),
            db.scalar(select(func.count(ProductionTask.id))),
        )
    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        response = client.get("/api/mobile/erp/production/recent")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        payload = response.json()
        assert payload["read_only"] is True
        assert payload["period"] == "3d"
        assert payload["count"] == 1
        row = payload["items"][0]
        assert row["receipt_item_id"] == ids["visible_fact"]
        assert row["material_state"] == "材料未齐"
        assert row["remaining_quantity"] == 40
        assert row["board_length_mm"] == "800"
        assert row["board_width_mm"] == "600"
        assert row["crease_values_mm"] == [120, 310, 120]
        assert row["board_direction_note"] == "长边顺瓦楞方向"
        assert row["drawing_path"].startswith("/api/orders/items/")
        assert len(row["production_tasks"]) == 1
        task = row["production_tasks"][0]
        assert task["status_text"] == "可以生产"
        assert task["current_producible_quantity"] == 60
        assert task["mold_location"] == "M1-R02"
        assert task["production_process"] == "模切后检查压线"
        assert "MOBILE-LINER-01" not in response.text
        response_text = response.text
        assert "SECRET-SUPPLIER-MATERIAL" not in response_text
        assert "SECRET-SUPPLIER" not in response_text

    with factory() as db:
        after = (
            db.scalar(select(func.count(IncomingReceiptItem.id))),
            db.scalar(select(func.count(ProductionTask.id))),
        )
    assert after == before


def test_recent_production_permission_and_custom_period_fail_closed(
    mobile_production_app,
) -> None:
    app, _factory, _ids = mobile_production_app
    with TestClient(app) as client:
        _login(client, "mobile-sales")
        denied = client.get("/api/mobile/erp/production/recent")
        assert denied.status_code == 403

    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        missing = client.get(
            "/api/mobile/erp/production/recent",
            params={"period": "custom"},
        )
        assert missing.status_code == 422
        backwards = client.get(
            "/api/mobile/erp/production/recent",
            params={
                "period": "custom",
                "date_from": "2026-08-02",
                "date_to": "2026-08-01",
            },
        )
        assert backwards.status_code == 422


def test_mobile_production_ui_is_strictly_read_only_and_preserves_context() -> None:
    for text in (
        "近 3 天生产状态",
        "生产",
        "材料未齐",
        "计划加工",
        "查看图纸",
        "返回生产资料",
        "本页只能查看",
    ):
        assert text in MOBILE_HTML
    assert "Production cards and scroll position remain untouched" in MOBILE_HTML
    production = MOBILE_HTML.split("function productionStationCard", 1)[1].split("async function initialize", 1)[0]
    assert "apiPost(" not in production
    assert "fetch(" not in production
    assert 'fetch("/api/auth/logout"' in MOBILE_HTML
    assert 'method: "PUT"' not in production
    assert 'method: "DELETE"' not in production


def test_completed_received_task_remains_visible_as_waiting_delivery(
    mobile_production_app,
) -> None:
    app, factory, ids = mobile_production_app
    from app.models.production import ProductionTask

    with factory() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == ids["visible_item"]
            )
        )
        assert task is not None
        task.status = "completed"
        db.commit()

    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        response = client.get("/api/mobile/erp/production/recent")
        assert response.status_code == 200, response.text
        tasks = response.json()["items"][0]["production_tasks"]
        assert len(tasks) == 1
        assert tasks[0]["status"] == "completed"
        assert tasks[0]["status_text"] == "已完工待送"
        assert tasks[0]["is_fully_delivered"] is False


def test_partial_receipts_show_one_task_card_using_the_latest_receipt(
    mobile_production_app,
) -> None:
    app, factory, ids = mobile_production_app
    from app.core.time_contract import utc_now_naive
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.user import User

    with factory() as db:
        item = db.get(OrderItem, ids["visible_item"])
        receiver = db.scalar(select(User).where(User.username == "mobile-workshop"))
        assert item is not None and receiver is not None
        receipt = IncomingReceipt(
            receipt_number="REC-VISIBLE-PART-2",
            status="posted",
            received_at=utc_now_naive(),
            received_by=receiver.id,
            idempotency_key="mobile-receipt-visible-part-2",
        )
        db.add(receipt)
        db.flush()
        fact = IncomingReceiptItem(
            receipt_id=receipt.id,
            order_id=item.order_id,
            order_item_id=item.id,
            planned_quantity=100,
            received_quantity=20,
            cumulative_received_quantity=80,
            variance_quantity=-20,
            variance_type="short",
            resolution_status="pending",
            resolution_action="await_supplier",
            status="posted",
        )
        db.add(fact)
        db.commit()
        latest_fact_id = int(fact.id)

    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        response = client.get("/api/mobile/erp/production/recent")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["count"] == 1
        assert payload["items"][0]["receipt_item_id"] == latest_fact_id
        assert payload["items"][0]["received_quantity"] == 20
        assert payload["items"][0]["cumulative_received_quantity"] == 80

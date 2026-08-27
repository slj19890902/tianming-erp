from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.orders import router as orders_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User


PASSWORD = "123456"
INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


@pytest.fixture()
def order_summary_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "order-list-summary.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="summary-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="摘要管理员",
            must_change_password=False,
            customer_access_mode="all",
        )
        scoped = User(
            username="summary-scoped",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="摘要受限员工",
            must_change_password=False,
            customer_access_mode="selected",
        )
        workshop = User(
            username="summary-workshop",
            password_hash=hash_password(PASSWORD),
            role="workshop",
            real_name="摘要车间员工",
            must_change_password=False,
            customer_access_mode="all",
        )
        allowed_customer = Customer(name="摘要客户甲")
        denied_customer = Customer(name="摘要客户乙")
        db.add_all([admin, scoped, workshop, allowed_customer, denied_customer])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(
                    user_id=scoped.id,
                    customer_id=allowed_customer.id,
                    assigned_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="orders.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
            ]
        )

        def add_order(customer: Customer, suffix: str, item_count: int) -> int:
            product = Product(
                customer_id=customer.id,
                product_code=f"SUMMARY-{suffix}",
                customer_material_code=f"MAT-{suffix}",
                product_name=f"摘要测试纸箱 {suffix}",
                box_style="普通箱",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"TM-SUMMARY-{suffix}",
                customer_id=customer.id,
                customer_po=f"PO-SUMMARY-{suffix}",
                order_date=date(2026, 8, 5),
                delivery_date=date(2026, 8, 12),
                status="pending_confirmation",
                payment_status="unpaid",
                total_amount=Decimal(item_count * 10),
                remark=f"订单备注 {suffix}",
            )
            db.add(order)
            db.flush()
            for index in range(1, item_count + 1):
                db.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=product.id,
                        item_sequence=index,
                        item_order_number=f"{order.order_number}-{index:03d}",
                        quantity=10,
                        delivered_quantity=0,
                        unit_price=Decimal("1"),
                        subtotal=Decimal("10"),
                        material_status="received",
                        snapshot_product_code=f"SUMMARY-{suffix}-{index:02d}",
                        snapshot_product_name=f"摘要测试纸箱 {suffix} 第 {index} 款",
                        snapshot_spec="400×300×200",
                        snapshot_material="A=A",
                        snapshot_production_notes="印刷后模切并粘箱",
                    )
                )
            db.flush()
            return order.id

        ids = {
            "allowed": add_order(allowed_customer, "A", 20),
            "denied": add_order(denied_customer, "B", 1),
        }
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, engine
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def test_order_summary_is_small_and_default_response_remains_full(order_summary_app) -> None:
    app, ids, _engine = order_summary_app
    with TestClient(app) as client:
        _login(client, "summary-admin")
        full = client.get(
            "/api/orders",
            params={"scope": "all", "customer_id": 1, "page": 1, "page_size": 25},
        )
        summary = client.get(
            "/api/orders",
            params={
                "scope": "all",
                "customer_id": 1,
                "page": 1,
                "page_size": 25,
                "detail_level": "summary",
            },
        )

    assert full.status_code == 200, full.text
    assert summary.status_code == 200, summary.text
    full_row = next(row for row in full.json()["items"] if row["id"] == ids["allowed"])
    summary_row = next(
        row for row in summary.json()["items"] if row["id"] == ids["allowed"]
    )
    assert len(full_row["items"]) == 20
    assert "snapshot_production_notes" in full_row["items"][0]
    assert "items" not in summary_row
    assert summary_row["item_count"] == 20
    assert summary_row["total_quantity"] == 200
    assert summary_row["all_material_received"] is True
    assert summary_row["business_remaining_quantity"] == 200
    for field in (
        "customer_id",
        "customer_name",
        "customer_po",
        "order_date",
        "delivery_date",
        "total_amount",
        "business_status",
        "business_delivery_progress",
    ):
        assert summary_row[field] == full_row[field]
    assert len(summary.content) < len(full.content) * 0.4


def test_order_summary_keeps_customer_scope_and_avoids_detail_queries(order_summary_app) -> None:
    app, ids, engine = order_summary_app
    with TestClient(app) as client:
        _login(client, "summary-scoped")
        selects = 0
        writes = 0

        def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
            nonlocal selects, writes
            operation = statement.lstrip().split(None, 1)[0].upper()
            if operation in {"SELECT", "WITH"}:
                selects += 1
            if operation in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                writes += 1

        event.listen(engine, "before_cursor_execute", count_sql)
        try:
            response = client.get(
                "/api/orders",
                params={
                    "scope": "all",
                    "detail_level": "summary",
                    "page": 1,
                    "page_size": 25,
                },
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_sql)

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [ids["allowed"]]
    assert writes == 0
    assert selects <= 30


def test_order_summary_keeps_workshop_prices_hidden(order_summary_app) -> None:
    app, _ids, _engine = order_summary_app
    with TestClient(app) as client:
        _login(client, "summary-workshop")
        response = client.get(
            "/api/orders",
            params={
                "scope": "all",
                "detail_level": "summary",
                "page": 1,
                "page_size": 25,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["items"]
    assert all("total_amount" not in row for row in response.json()["items"])


def _method_source(start: str, end: str) -> str:
    return INDEX.split(start, 1)[1].split(end, 1)[0]


def test_order_page_requests_summary_and_lazily_loads_group_details() -> None:
    loader = _method_source("async loadOrders() {", "async autoReleaseReadyRequisitionHolds")
    toggle = _method_source("async ensureOrderGroupDetail(group", "async openOrderGroupDetail")
    template = INDEX.split('<table class="order-group-table">', 1)[1].split(
        '<template v-else-if="activePage === \'orders_legacy\'">', 1
    )[0]

    assert 'params.detail_level = "summary"' in loader
    assert "this.orderGroupDetails = {}" in loader
    assert "this.orderGroupDetailErrors = {}" in loader
    assert 'axios.get("/api/orders/group-detail"' in toggle
    assert "orderGroupDetailRequests.get(key)" in toggle
    assert "existing.promise" in toggle
    assert "this.orderGroupDetails[key]" in toggle
    assert "订单明细加载失败" in toggle
    assert 'typeof group === "string"' in toggle
    assert '@click="toggleOrderGroup(group)"' in template
    assert "orderGroupExpandedOrders(group)" in template
    assert "正在加载订单明细" in template
    assert "重新加载" in template


def test_order_editor_fetches_complete_group_before_editing() -> None:
    editor = _method_source("async openOrderEditor(group)", "async dangerRollbackOrderGroup")

    assert "await this.ensureOrderGroupDetail(group)" in editor
    assert "detail.orders" in editor
    assert "remark: first.remark" in editor


def test_order_summary_response_does_not_expose_full_item_payload_shape() -> None:
    summary_fields = {
        "item_count",
        "total_quantity",
        "all_material_received",
        "business_remaining_quantity",
    }
    assert "detail_level: Literal[\"full\", \"summary\"]" in Path(
        "app/api/orders.py"
    ).read_text(encoding="utf-8")
    assert len(summary_fields) == 4

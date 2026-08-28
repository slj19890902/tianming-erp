from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def order_reminder_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.finance import ReturnReceipt
    from app.models.fulfillment_reminder import FulfillmentReminder
    from app.models.product import Product
    from app.models.user import User
    from app.services import fulfillment_reminders as reminder_service

    monkeypatch.setattr(reminder_service, "beijing_today", lambda: date(2026, 8, 16))

    engine = create_sqlite_engine(tmp_path / "p1-65c.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    ids: dict[str, int] = {}
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
            username="order-scoped",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="订单文员",
            display_name="订单文员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(customer_number=6561, customer_code="P165C-A", name="苏州思迈尔包装有限公司")
        customer_b = Customer(customer_number=6562, customer_code="P165C-B", name="昆山华诚电子有限公司")
        session.add_all([admin, scoped, customer_a, customer_b])
        session.flush()
        session.add_all([
            UserPermissionOverride(
                user_id=scoped.id,
                permission_code="orders.view",
                is_allowed=True,
                granted_by=admin.id,
            ),
            UserCustomerScope(user_id=scoped.id, customer_id=customer_a.id, assigned_by=admin.id),
        ])
        product_a = Product(
            customer_id=customer_a.id,
            product_code="A-BOX-01",
            customer_material_code="A-BOX-01",
            product_name="五层加强纸箱",
            legacy_material_text="K=A-BC",
            box_category="normal",
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="B-BOX-01",
            customer_material_code="B-BOX-01",
            product_name="物流周转箱",
            legacy_material_text="A=A-B",
            box_category="normal",
        )
        session.add_all([product_a, product_b])
        session.flush()

        delivery_a = Delivery(delivery_number="DH-P165C-A", customer_id=customer_a.id, delivery_date=date(2026, 8, 15), status="dispatched", total_quantity=1)
        delivery_b = Delivery(delivery_number="DH-P165C-B", customer_id=customer_b.id, delivery_date=date(2026, 8, 15), status="dispatched", total_quantity=1)
        session.add_all([delivery_a, delivery_b])
        session.flush()
        receipt_a = ReturnReceipt(delivery_id=delivery_a.id, actual_received_date=date(2026, 8, 16), signed_by="王经理", status="confirmed", created_by=admin.id)
        receipt_b = ReturnReceipt(delivery_id=delivery_b.id, actual_received_date=date(2026, 8, 16), signed_by="李经理", status="confirmed", created_by=admin.id)
        session.add_all([receipt_a, receipt_b])
        session.flush()

        def reminder(
            *,
            customer=customer_a,
            product=None,
            delivery=delivery_a,
            receipt=receipt_a,
            scope="customer",
            reminder_type="production_attention",
            remind_on=date(2026, 8, 16),
            status="active",
            source_valid=True,
            content="生产前核对印刷色序",
        ):
            row = FulfillmentReminder(
                source_return_receipt_id=receipt.id,
                source_return_receipt_id_snapshot=receipt.id,
                source_delivery_id_snapshot=delivery.id,
                source_delivery_number_snapshot=delivery.delivery_number,
                source_received_date_snapshot=receipt.actual_received_date,
                source_valid=source_valid,
                customer_id=customer.id,
                customer_name_snapshot=customer.name,
                product_id=product.id if product else None,
                product_id_snapshot=product.id if product else None,
                product_code_snapshot=product.product_code if product else None,
                product_name_snapshot=product.product_name if product else None,
                scope_type=scope,
                reminder_type=reminder_type,
                content=content,
                cadence="continuous",
                remind_on=remind_on,
                status=status,
                version=1,
                created_by=admin.id,
                created_by_name_snapshot="管理员",
            )
            session.add(row)
            session.flush()
            return row

        customer_row = reminder(content="开机前先核对客户回单交代")
        product_row = reminder(product=product_a, scope="product", content="此款先做首件确认")
        reminder(reminder_type="delivery_attention", content="只在送货提醒")
        reminder(scope="receipt", content="只在回单查看")
        reminder(remind_on=date(2026, 8, 17), content="未来提醒")
        reminder(status="resolved", content="已处理")
        reminder(source_valid=False, content="来源已取消")
        hidden = reminder(customer=customer_b, product=product_b, delivery=delivery_b, receipt=receipt_b, scope="product", content="他客提醒")
        session.commit()
        ids.update(
            customer_a=customer_a.id,
            customer_b=customer_b.id,
            product_a=product_a.id,
            product_b=product_b.id,
            customer_row=customer_row.id,
            product_row=product_row.id,
            hidden=hidden.id,
        )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory, engine, ids


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "RolePass123!"})
    assert response.status_code == 200, response.text


def test_order_projection_is_current_internal_batched_and_scoped(order_reminder_app) -> None:
    app, _session_factory, _engine, ids = order_reminder_app
    with TestClient(app) as client:
        _login(client, "order-scoped")
        response = client.get("/api/orders/fulfillment-reminders", params={"customer_id": ids["customer_a"]})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 2
        assert {row["id"] for row in data["items"]} == {ids["customer_row"], ids["product_row"]}
        assert {row["reminder_type"] for row in data["items"]} == {"production_attention"}
        product = next(row for row in data["items"] if row["scope_type"] == "product")
        assert product["product_id"] == ids["product_a"]
        assert client.get("/api/orders/fulfillment-reminders", params={"customer_id": ids["customer_b"]}).status_code == 403


def test_task_projection_matches_customer_and_exact_product_without_n_plus_one(order_reminder_app) -> None:
    from app.services.fulfillment_reminders import annotate_production_reminders

    _app, session_factory, engine, ids = order_reminder_app

    def read(items: list[dict]) -> tuple[int, list[dict]]:
        queries = 0

        def before_cursor_execute(*_args):
            nonlocal queries
            queries += 1

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            with session_factory() as session:
                result = annotate_production_reminders(session, items)
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
        return queries, result

    one_count, one = read([{"id": 1, "customer_id": ids["customer_a"], "product_id": ids["product_a"]}])
    many_count, many = read([
        {"id": index, "customer_id": ids["customer_a"], "product_id": ids["product_a"] if index % 2 else 999999}
        for index in range(1, 101)
    ])
    assert one_count == many_count == 1
    assert {row["id"] for row in one[0]["fulfillment_reminders"]} == {ids["customer_row"], ids["product_row"]}
    mismatched = next(row for row in many if row["product_id"] == 999999)
    assert [row["id"] for row in mismatched["fulfillment_reminders"]] == [ids["customer_row"]]


def test_print_projection_filter_keeps_customer_and_exact_component_product(order_reminder_app) -> None:
    from app.services.fulfillment_reminders import (
        matching_production_reminders,
        production_reminders_by_customer,
    )

    _app, session_factory, _engine, ids = order_reminder_app
    with session_factory() as session:
        rows = production_reminders_by_customer(session, {ids["customer_a"]})[ids["customer_a"]]
        formal = matching_production_reminders(rows, product_ids={ids["product_a"]})
        temporary = matching_production_reminders(rows, product_ids=set())
    assert {row["id"] for row in formal} == {ids["customer_row"], ids["product_row"]}
    assert [row["id"] for row in temporary] == [ids["customer_row"]]

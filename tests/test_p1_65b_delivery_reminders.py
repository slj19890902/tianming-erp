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
def delivery_reminder_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deliveries import router as deliveries_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
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

    monkeypatch.setattr(
        reminder_service,
        "beijing_today",
        lambda: date(2026, 8, 16),
    )

    engine = create_sqlite_engine(tmp_path / "p1-65b.sqlite3")
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
            username="delivery-scoped",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="送货文员",
            display_name="送货文员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(
            customer_number=6551,
            customer_code="P165B-A",
            name="苏州思迈尔包装有限公司",
        )
        customer_b = Customer(
            customer_number=6552,
            customer_code="P165B-B",
            name="昆山华诚电子有限公司",
        )
        session.add_all([admin, scoped, customer_a, customer_b])
        session.flush()
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="deliveries.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="finance.execute",
                    is_allowed=False,
                    granted_by=admin.id,
                ),
                UserCustomerScope(
                    user_id=scoped.id,
                    customer_id=customer_a.id,
                    assigned_by=admin.id,
                ),
            ]
        )
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

        def source(customer: Customer, suffix: str) -> tuple[Delivery, ReturnReceipt]:
            delivery = Delivery(
                delivery_number=f"DH-P165B-{suffix}",
                customer_id=customer.id,
                delivery_date=date(2026, 8, 15),
                status="dispatched",
                total_quantity=1,
            )
            session.add(delivery)
            session.flush()
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=date(2026, 8, 16),
                signed_by="王经理",
                status="confirmed",
                created_by=admin.id,
            )
            session.add(receipt)
            session.flush()
            return delivery, receipt

        delivery_a, receipt_a = source(customer_a, "A")
        delivery_b, receipt_b = source(customer_b, "B")

        def reminder(
            *,
            customer: Customer = customer_a,
            product: Product | None = None,
            delivery: Delivery = delivery_a,
            receipt: ReturnReceipt = receipt_a,
            scope: str = "customer",
            reminder_type: str = "delivery_attention",
            cadence: str = "one_time",
            remind_on: date | None = date(2026, 8, 16),
            status: str = "active",
            source_valid: bool = True,
            content: str = "送货前先联系仓库",
        ) -> FulfillmentReminder:
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
                suggested_quantity=2 if reminder_type == "replenishment" else None,
                cadence=cadence,
                remind_on=remind_on,
                status=status,
                version=1,
                created_by=admin.id,
                created_by_name_snapshot="管理员",
            )
            session.add(row)
            session.flush()
            return row

        due_customer = reminder(content="卸货前联系仓库")
        due_product = reminder(
            product=product_a,
            scope="product",
            reminder_type="replenishment",
            content="下次送货补 2 只",
        )
        reminder(scope="receipt", content="仅本次回单内部记录")
        reminder(reminder_type="production_attention", content="生产前核对印刷")
        reminder(remind_on=date(2026, 8, 17), content="明日才提醒")
        reminder(status="resolved", content="已经处理")
        reminder(source_valid=False, content="来源已取消")
        hidden_customer = reminder(
            customer=customer_b,
            product=product_b,
            delivery=delivery_b,
            receipt=receipt_b,
            scope="product",
            content="他客提醒",
        )
        session.commit()
        ids.update(
            customer_a=customer_a.id,
            customer_b=customer_b.id,
            product_a=product_a.id,
            due_customer=due_customer.id,
            due_product=due_product.id,
            hidden_customer=hidden_customer.id,
        )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory, engine, ids


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def test_delivery_projection_is_batched_due_internal_and_customer_scoped(
    delivery_reminder_app,
) -> None:
    app, _session_factory, _engine, ids = delivery_reminder_app
    with TestClient(app) as client:
        _login(client, "delivery-scoped")
        response = client.get(
            "/api/deliveries/fulfillment-reminders",
            params={"customer_id": ids["customer_a"], "page": 1, "page_size": 100},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 2
        assert {row["id"] for row in data["items"]} == {
            ids["due_customer"],
            ids["due_product"],
        }
        assert {row["scope_type"] for row in data["items"]} == {"customer", "product"}
        assert {row["reminder_type"] for row in data["items"]} == {
            "delivery_attention",
            "replenishment",
        }
        product = next(row for row in data["items"] if row["scope_type"] == "product")
        assert product["product_id"] == ids["product_a"]
        assert product["suggested_quantity"] == "2.000"

        hidden = client.get(
            "/api/deliveries/fulfillment-reminders",
            params={"customer_id": ids["customer_b"]},
        )
        assert hidden.status_code == 403
        cannot_resolve = client.post(
            f"/api/finance/fulfillment-reminders/{ids['due_customer']}/resolve",
            json={"expected_version": 1, "idempotency_key": "p165b-no-finance"},
        )
        assert cannot_resolve.status_code == 403


def test_delivery_reminder_query_count_does_not_grow_with_rows(
    delivery_reminder_app,
) -> None:
    from app.models.fulfillment_reminder import FulfillmentReminder
    from app.services.fulfillment_reminders import list_delivery_reminders

    _app, session_factory, engine, ids = delivery_reminder_app
    with session_factory() as session:
        template = session.get(FulfillmentReminder, ids["due_customer"])
        for index in range(110):
            session.add(
                FulfillmentReminder(
                    source_return_receipt_id=template.source_return_receipt_id,
                    source_return_receipt_id_snapshot=template.source_return_receipt_id_snapshot,
                    source_delivery_id_snapshot=template.source_delivery_id_snapshot,
                    source_delivery_number_snapshot=template.source_delivery_number_snapshot,
                    source_received_date_snapshot=template.source_received_date_snapshot,
                    source_valid=True,
                    customer_id=template.customer_id,
                    customer_name_snapshot=template.customer_name_snapshot,
                    scope_type="customer",
                    reminder_type="delivery_attention",
                    content=f"批量提醒 {index}",
                    cadence="continuous",
                    remind_on=None,
                    status="active",
                    version=1,
                    created_by=template.created_by,
                    created_by_name_snapshot=template.created_by_name_snapshot,
                )
            )
        session.commit()

    def counted(page_size: int) -> tuple[int, dict]:
        count = 0

        def before_cursor_execute(*_args):
            nonlocal count
            count += 1

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            with session_factory() as session:
                data = list_delivery_reminders(
                    session,
                    customer_id=ids["customer_a"],
                    page=1,
                    page_size=page_size,
                )
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
        return count, data

    one_count, one = counted(1)
    hundred_count, hundred = counted(100)
    assert one_count == hundred_count == 2
    assert len(one["items"]) == 1
    assert len(hundred["items"]) == 100
    assert hundred["total"] == 112


def test_admin_can_resolve_one_time_reminder_with_existing_cas_and_replay(
    delivery_reminder_app,
) -> None:
    app, _session_factory, _engine, ids = delivery_reminder_app
    payload = {"expected_version": 1, "idempotency_key": "p165b-resolve-once"}
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.post(
            f"/api/finance/fulfillment-reminders/{ids['due_customer']}/resolve",
            json=payload,
        )
        replay = client.post(
            f"/api/finance/fulfillment-reminders/{ids['due_customer']}/resolve",
            json=payload,
        )
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert first.json()["status"] == "resolved"
        assert first.json()["version"] == 2

from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def reminder_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-65a.sqlite3")
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
        finance = User(
            username="finance",
            password_hash=hash_password("RolePass123!"),
            role="finance",
            real_name="回单员",
            display_name="回单员",
            must_change_password=False,
        )
        scoped = User(
            username="scoped",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="受限回单员",
            display_name="受限回单员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(
            customer_number=651,
            customer_code="P165A",
            name="苏州思迈尔包装有限公司",
        )
        customer_b = Customer(
            customer_number=652,
            customer_code="P165B",
            name="昆山华诚电子有限公司",
        )
        session.add_all([admin, finance, scoped, customer_a, customer_b])
        session.flush()
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="finance.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="finance.execute",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserCustomerScope(
                    user_id=scoped.id,
                    customer_id=customer_a.id,
                    assigned_by=admin.id,
                ),
            ]
        )
        product_a1 = Product(
            customer_id=customer_a.id,
            product_code="A-001",
            customer_material_code="A-001",
            product_name="五层加强纸箱",
            legacy_material_text="K=A-BC",
            box_category="normal",
        )
        product_a2 = Product(
            customer_id=customer_a.id,
            product_code="A-002",
            customer_material_code="A-002",
            product_name="彩印内盒",
            legacy_material_text="A=B-E",
            box_category="die_cut",
        )
        product_b1 = Product(
            customer_id=customer_b.id,
            product_code="B-001",
            customer_material_code="B-001",
            product_name="物流周转箱",
            legacy_material_text="A=A-B",
            box_category="normal",
        )
        session.add_all([product_a1, product_a2, product_b1])
        session.flush()

        def add_delivery(
            *,
            customer: Customer,
            product: Product,
            number: str,
            order_number: str,
        ) -> tuple[Order, OrderItem, Delivery, DeliveryItem]:
            order = Order(
                order_number=order_number,
                customer_id=customer.id,
                customer_po=f"PO-{order_number}",
                order_date=date(2026, 8, 12),
                delivery_date=date(2026, 8, 15),
                status="delivered",
                payment_status="unpaid",
                total_amount=Decimal("30"),
            )
            session.add(order)
            session.flush()
            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=10,
                delivered_quantity=10,
                unit_price=Decimal("3"),
                subtotal=Decimal("30"),
                material_status="received",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="520×350×300mm",
                snapshot_material=product.legacy_material_text,
            )
            session.add(order_item)
            session.flush()
            delivery = Delivery(
                delivery_number=number,
                customer_id=customer.id,
                delivery_date=date(2026, 8, 15),
                status="dispatched",
                total_quantity=10,
                dispatched_at=datetime(2026, 8, 15, 9, 0),
            )
            session.add(delivery)
            session.flush()
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=10,
                remarks="客户可见送货备注",
            )
            session.add(delivery_item)
            session.flush()
            return order, order_item, delivery, delivery_item

        _order_a1, _order_item_a1, delivery_a1, delivery_item_a1 = add_delivery(
            customer=customer_a,
            product=product_a1,
            number="DH-20260815-A1",
            order_number="SO-20260815-A1",
        )
        _order_a2, _order_item_a2, delivery_a2, delivery_item_a2 = add_delivery(
            customer=customer_a,
            product=product_a1,
            number="DH-20260815-A2",
            order_number="SO-20260815-A2",
        )
        _order_b1, _order_item_b1, delivery_b1, delivery_item_b1 = add_delivery(
            customer=customer_b,
            product=product_b1,
            number="DH-20260815-B1",
            order_number="SO-20260815-B1",
        )
        session.commit()
        ids.update(
            admin=admin.id,
            customer_a=customer_a.id,
            customer_b=customer_b.id,
            product_a1=product_a1.id,
            product_a2=product_a2.id,
            product_b1=product_b1.id,
            delivery_a1=delivery_a1.id,
            delivery_item_a1=delivery_item_a1.id,
            delivery_a2=delivery_a2.id,
            delivery_item_a2=delivery_item_a2.id,
            delivery_b1=delivery_b1.id,
            delivery_item_b1=delivery_item_b1.id,
        )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory, ids


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _receipt_payload(ids: dict[str, int], *, delivery_suffix: str = "a1") -> dict:
    return {
        "delivery_id": ids[f"delivery_{delivery_suffix}"],
        "actual_received_date": "2026-08-16",
        "signed_by": "王经理",
        "items": [
            {
                "delivery_item_id": ids[f"delivery_item_{delivery_suffix}"],
                "actual_received_quantity": 10,
            }
        ],
    }


def _draft(
    *,
    scope_type: str,
    reminder_type: str,
    content: str,
    product_id: int | None = None,
    cadence: str = "one_time",
    quantity: str | None = None,
) -> dict:
    return {
        "scope_type": scope_type,
        "reminder_type": reminder_type,
        "content": content,
        "product_id": product_id,
        "cadence": cadence,
        "suggested_quantity": quantity,
        "remind_on": "2026-08-16",
    }


def test_receipt_and_four_reminder_scopes_commit_atomically_and_replay(
    reminder_api_app,
) -> None:
    from app.models.delivery import DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem
    from app.models.fulfillment_reminder import (
        FulfillmentReminder,
        FulfillmentReminderMutation,
    )

    app, session_factory, ids = reminder_api_app
    payload = {
        **_receipt_payload(ids),
        "reminder_bundle_idempotency_key": "p1-65a-bundle-four-scopes",
        "reminders": [
            _draft(
                scope_type="receipt",
                reminder_type="replenishment",
                content="本次短缺另约时间补送",
                quantity="2",
            ),
            _draft(
                scope_type="customer",
                reminder_type="delivery_attention",
                content="下次送货提前电话联系仓库",
                cadence="continuous",
            ),
            _draft(
                scope_type="product",
                reminder_type="production_attention",
                content="此款下次生产注意印刷位置",
                product_id=ids["product_a1"],
            ),
            _draft(
                scope_type="product",
                reminder_type="other",
                content="客户同时交代另一常用箱包装方式",
                product_id=ids["product_a2"],
            ),
        ],
    }
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post("/api/finance/return_receipts", json=payload)
        replay = client.post("/api/finance/return_receipts", json=payload)
        changed_payload = {
            **payload,
            "reminders": [
                {**payload["reminders"][0], "content": "改成不同内容"},
                *payload["reminders"][1:],
            ],
        }
        conflict = client.post(
            "/api/finance/return_receipts",
            json=changed_payload,
        )
        listed = client.get(
            f"/api/finance/return_receipts/{created.json()['id']}/reminders"
        )

    assert created.status_code == 201, created.text
    assert replay.status_code == 201, replay.text
    assert replay.json() == created.json()
    assert conflict.status_code == 409
    assert len(created.json()["created_reminders"]) == 4
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 4
    assert {row["scope_type"] for row in listed.json()["items"]} == {
        "receipt",
        "customer",
        "product",
    }
    assert {
        row["product_id"]
        for row in listed.json()["items"]
        if row["scope_type"] == "product"
    } == {ids["product_a1"], ids["product_a2"]}
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ReturnReceipt)) == 1
        assert session.scalar(select(func.count()).select_from(ReturnReceiptItem)) == 1
        assert session.scalar(select(func.count()).select_from(FulfillmentReminder)) == 4
        assert (
            session.scalar(select(func.count()).select_from(FulfillmentReminderMutation))
            == 1
        )
        assert session.get(DeliveryItem, ids["delivery_item_a1"]).remarks == "客户可见送货备注"


def test_invalid_cross_customer_product_rolls_back_receipt_and_all_reminders(
    reminder_api_app,
) -> None:
    from app.models.finance import ReturnReceipt
    from app.models.fulfillment_reminder import FulfillmentReminder

    app, session_factory, ids = reminder_api_app
    payload = {
        **_receipt_payload(ids, delivery_suffix="a2"),
        "reminder_bundle_idempotency_key": "p1-65a-cross-customer-rollback",
        "reminders": [
            _draft(
                scope_type="product",
                reminder_type="production_attention",
                content="不能关联另一客户的产品",
                product_id=ids["product_b1"],
            )
        ],
    }
    with TestClient(app) as client:
        _login(client, "finance")
        response = client.post("/api/finance/return_receipts", json=payload)

    assert response.status_code == 404
    with session_factory() as session:
        assert session.scalar(
            select(func.count(ReturnReceipt.id)).where(
                ReturnReceipt.delivery_id == ids["delivery_a2"]
            )
        ) == 0
        assert session.scalar(select(func.count()).select_from(FulfillmentReminder)) == 0


def test_reminder_lifecycle_version_idempotency_and_private_audit(
    reminder_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.fulfillment_reminder import FulfillmentReminder

    app, session_factory, ids = reminder_api_app
    secret_text = "内部：客户下次补送前先联系张主管"
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(ids),
        )
        assert receipt.status_code == 201, receipt.text
        receipt_id = receipt.json()["id"]
        create_payload = {
            **_draft(
                scope_type="customer",
                reminder_type="delivery_attention",
                content=secret_text,
                cadence="continuous",
            ),
            "idempotency_key": "p1-65a-create-reminder",
        }
        created = client.post(
            f"/api/finance/return_receipts/{receipt_id}/reminders",
            json=create_payload,
        )
        create_replay = client.post(
            f"/api/finance/return_receipts/{receipt_id}/reminders",
            json=create_payload,
        )
        reminder_id = created.json()["id"]
        update_payload = {
            **_draft(
                scope_type="product",
                reminder_type="production_attention",
                content="内部：印刷位置调整后再生产",
                product_id=ids["product_a1"],
                quantity="5",
            ),
            "expected_version": 1,
            "idempotency_key": "p1-65a-update-reminder",
        }
        updated = client.put(
            f"/api/finance/fulfillment-reminders/{reminder_id}",
            json=update_payload,
        )
        stale = client.put(
            f"/api/finance/fulfillment-reminders/{reminder_id}",
            json={**update_payload, "idempotency_key": "p1-65a-stale-update"},
        )
        resolved_payload = {
            "expected_version": 2,
            "idempotency_key": "p1-65a-resolve-reminder",
        }
        resolved = client.post(
            f"/api/finance/fulfillment-reminders/{reminder_id}/resolve",
            json=resolved_payload,
        )
        resolved_replay = client.post(
            f"/api/finance/fulfillment-reminders/{reminder_id}/resolve",
            json=resolved_payload,
        )
        history = client.get(
            f"/api/finance/return_receipts/{receipt_id}/reminders",
            params={"history": "true"},
        )

    assert created.status_code == 201, created.text
    assert create_replay.json() == created.json()
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert stale.status_code == 409
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["version"] == 3
    assert resolved_replay.json() == resolved.json()
    assert history.json()["total"] == 1
    with session_factory() as session:
        reminder = session.get(FulfillmentReminder, reminder_id)
        assert reminder.status == "resolved"
        logs = session.scalars(
            select(OperationLog).where(
                OperationLog.resource == "FulfillmentReminder"
            )
        ).all()
        assert [row.action_code for row in logs] == [
            "FULFILLMENT_REMINDER_CREATED",
            "FULFILLMENT_REMINDER_UPDATED",
            "FULFILLMENT_REMINDER_RESOLVED",
        ]
        serialized_logs = "\n".join(
            f"{row.details or ''}\n{row.extra_json or ''}\n{row.description or ''}"
            for row in logs
        )
        assert secret_text not in serialized_logs
        assert "印刷位置调整后再生产" not in serialized_logs


def test_receipt_cancel_preserves_reminder_and_marks_source_invalid_then_restores(
    reminder_api_app,
) -> None:
    from app.models.fulfillment_reminder import FulfillmentReminder
    from app.models.product import Product

    app, session_factory, ids = reminder_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(ids),
        )
        receipt_id = receipt.json()["id"]
        created = client.post(
            f"/api/finance/return_receipts/{receipt_id}/reminders",
            json={
                **_draft(
                    scope_type="product",
                    reminder_type="other",
                    content="保留来源追溯",
                    product_id=ids["product_a1"],
                ),
                "idempotency_key": "p1-65a-source-invalidation",
            },
        )
        reminder_id = created.json()["id"]
        cancelled = client.post(
            f"/api/finance/return_receipts/{receipt_id}/cancel"
        )
        invalid_list = client.get(
            f"/api/finance/return_receipts/{receipt_id}/reminders"
        )
        with session_factory() as session:
            product = session.get(Product, ids["product_a1"])
            product.is_active = False
            product.deleted_at = datetime(2026, 8, 16, 12, 0)
            session.commit()
        reopened = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-08-17",
                "signed_by": "李经理",
                "items": [
                    {
                        "delivery_item_id": ids["delivery_item_a1"],
                        "actual_received_quantity": 10,
                    }
                ],
            },
        )
        restored_list = client.get(
            f"/api/finance/return_receipts/{receipt_id}/reminders"
        )

    assert cancelled.status_code == 200, cancelled.text
    assert invalid_list.json()["items"][0]["source_valid"] is False
    assert invalid_list.json()["items"][0]["product_code"] == "A-001"
    assert reopened.status_code == 200, reopened.text
    restored = restored_list.json()["items"][0]
    assert restored["source_valid"] is True
    assert restored["source_received_date"] == "2026-08-17"
    assert restored["product_id"] == ids["product_a1"]
    assert restored["product_name"] == "五层加强纸箱"
    with session_factory() as session:
        reminder = session.get(FulfillmentReminder, reminder_id)
        assert reminder.status == "active"
        assert reminder.source_valid is True
        assert reminder.version == 3


def test_selected_customer_scope_is_applied_before_reminder_lookup(
    reminder_api_app,
) -> None:
    app, _, ids = reminder_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        receipt_b = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(ids, delivery_suffix="b1"),
        )
        assert receipt_b.status_code == 201, receipt_b.text
        reminder_b = client.post(
            f"/api/finance/return_receipts/{receipt_b.json()['id']}/reminders",
            json={
                **_draft(
                    scope_type="customer",
                    reminder_type="delivery_attention",
                    content="另一客户内部备忘",
                ),
                "idempotency_key": "p1-65a-other-customer-reminder",
            },
        )
        assert reminder_b.status_code == 201, reminder_b.text
        _login(client, "scoped")
        hidden_list = client.get(
            f"/api/finance/return_receipts/{receipt_b.json()['id']}/reminders"
        )
        hidden_direct = client.post(
            f"/api/finance/fulfillment-reminders/{reminder_b.json()['id']}/resolve",
            json={
                "expected_version": 1,
                "idempotency_key": "p1-65a-scope-denied-resolve",
            },
        )
        hidden_products = client.get(
            f"/api/finance/deliveries/{ids['delivery_b1']}/reminder-product-options"
        )
        own_products = client.get(
            f"/api/finance/deliveries/{ids['delivery_a1']}/reminder-product-options"
        )

    assert hidden_list.status_code == 404
    assert hidden_direct.status_code == 404
    assert hidden_products.status_code == 404
    assert own_products.status_code == 200
    assert {row["id"] for row in own_products.json()["items"]} == {
        ids["product_a1"],
        ids["product_a2"],
    }


def test_receipt_and_initial_reminders_roll_back_when_audit_fails(
    reminder_api_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import ReturnReceipt
    from app.models.fulfillment_reminder import FulfillmentReminder

    app, session_factory, ids = reminder_api_app

    def fail_reminder_audit(*_args, **_kwargs):
        raise RuntimeError("forced reminder audit failure")

    monkeypatch.setattr(
        "app.services.fulfillment_reminders.append_audit_event",
        fail_reminder_audit,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "finance")
        response = client.post(
            "/api/finance/return_receipts",
            json={
                **_receipt_payload(ids, delivery_suffix="a2"),
                "reminder_bundle_idempotency_key": "p1-65a-audit-failure",
                "reminders": [
                    _draft(
                        scope_type="customer",
                        reminder_type="delivery_attention",
                        content="这条内容必须随失败事务一起回滚",
                    )
                ],
            },
        )

    assert response.status_code == 500
    with session_factory() as session:
        assert session.scalar(
            select(func.count(ReturnReceipt.id)).where(
                ReturnReceipt.delivery_id == ids["delivery_a2"]
            )
        ) == 0
        assert session.scalar(select(func.count()).select_from(FulfillmentReminder)) == 0
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.resource == "FulfillmentReminder"
            )
        ) == 0


def test_reminder_list_query_count_does_not_grow_with_page_size(
    reminder_api_app,
) -> None:
    app, session_factory, ids = reminder_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(ids),
        )
        assert receipt.status_code == 201, receipt.text
        receipt_id = receipt.json()["id"]
        for index in range(20):
            created = client.post(
                f"/api/finance/return_receipts/{receipt_id}/reminders",
                json={
                    **_draft(
                        scope_type="customer",
                        reminder_type="other",
                        content=f"分页备忘 {index + 1}",
                    ),
                    "idempotency_key": f"p1-65a-query-{index + 1}",
                },
            )
            assert created.status_code == 201, created.text

        statements: list[str] = []
        engine = session_factory.kw["bind"]

        def record_select(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_select)
        try:
            statements.clear()
            one = client.get(
                f"/api/finance/return_receipts/{receipt_id}/reminders",
                params={"page_size": 1},
            )
            one_count = len(statements)
            statements.clear()
            twenty = client.get(
                f"/api/finance/return_receipts/{receipt_id}/reminders",
                params={"page_size": 20},
            )
            twenty_count = len(statements)
        finally:
            event.remove(engine, "before_cursor_execute", record_select)

    assert one.status_code == 200, one.text
    assert twenty.status_code == 200, twenty.text
    assert len(one.json()["items"]) == 1
    assert len(twenty.json()["items"]) == 20
    assert twenty_count == one_count

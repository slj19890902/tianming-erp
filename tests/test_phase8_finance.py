from __future__ import annotations

import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from json import loads
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def finance_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.deliveries import router as deliveries_router
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "finance.sqlite3")
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
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([*users, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="SME-001",
            customer_material_code="KH-001",
            product_name="五层加强纸箱",
            legacy_material_text="K=A-BC",
            box_category="normal",
            cost_unit_price=Decimal("2.70"),
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260613-001",
            customer_id=customer.id,
            customer_po="CPO-001",
            order_date=date(2026, 6, 1),
            delivery_date=date(2026, 6, 13),
            status="partially_delivered",
            payment_status="unpaid",
            total_amount=Decimal("360"),
        )
        session.add(order)
        session.flush()
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            delivered_quantity=80,
            unit_price=Decimal("3.60"),
            subtotal=Decimal("360"),
            material_status="received",
            snapshot_product_name="五层加强纸箱",
            snapshot_spec="520×350×300mm",
            snapshot_material="K=A-BC",
        )
        session.add(order_item)
        session.flush()
        delivery = Delivery(
            delivery_number="DH-20260613-001",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 13),
            vehicle_number="苏E·12345",
            status="dispatched",
            total_quantity=80,
            dispatched_at=datetime(2026, 6, 13, 9, 0, 0),
        )
        session.add(delivery)
        session.flush()
        session.add(
            DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=80,
                remarks="第一批",
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _race_requests(*requests):
    barrier = Barrier(len(requests) + 1)

    def run(request):
        barrier.wait()
        return request()

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        futures = [pool.submit(run, request) for request in requests]
        barrier.wait()
        return [future.result() for future in futures]


def _receipt_payload(reason: str | None = "压坏拒收 2 个") -> dict:
    return {
        "delivery_id": 1,
        "actual_received_date": "2026-06-14",
        "reconciliation_month": "2026-06",
        "signed_by": "王经理",
        "items": [
            {
                "delivery_item_id": 1,
                "actual_received_quantity": 78,
                "resolution_action": "continue_delivery",
                "difference_reason": reason,
            }
        ],
    }


def test_p1_89_default_month_is_server_month_and_create_replays_exactly(
    finance_api_app,
    monkeypatch,
) -> None:
    from app.api import finance as finance_api
    from app.models.finance import FinanceIdempotencyRecord, ReturnReceipt

    app, session_factory = finance_api_app
    monkeypatch.setattr(finance_api, "beijing_today", lambda: date(2026, 8, 22))
    payload = _receipt_payload()
    payload.pop("reconciliation_month")
    payload["idempotency_key"] = "p1-89-receipt-default-month"
    with TestClient(app) as client:
        _login(client, "finance")
        month_options = client.get("/api/finance/reconciliation-month-options")
        created = client.post("/api/finance/return_receipts", json=payload)
        replay = client.post("/api/finance/return_receipts", json=payload)
        changed = client.post(
            "/api/finance/return_receipts",
            json={**payload, "reconciliation_month": "2026-09"},
        )
        _login(client, "admin")
        other_actor = client.post("/api/finance/return_receipts", json=payload)

    assert month_options.status_code == 200, month_options.text
    assert month_options.json() == {
        "previous": "2026-07",
        "current": "2026-08",
        "next": "2026-09",
    }
    assert created.status_code == 201, created.text
    assert replay.status_code == 201, replay.text
    assert replay.json() == created.json()
    assert created.json()["reconciliation_month"] == "2026-08"
    assert created.json()["effective_reconciliation_month"] == "2026-08"
    assert created.json()["reconciliation_month_source"] == "explicit"
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "finance_idempotency_conflict"
    assert other_actor.status_code == 409
    assert other_actor.json()["detail"]["code"] == "finance_idempotency_conflict"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ReturnReceipt)) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(FinanceIdempotencyRecord)
            )
            == 1
        )


def test_p1_89_historical_null_month_keeps_old_delivery_cycle_membership(
    finance_api_app,
) -> None:
    from app.models.finance import ReturnReceipt

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json={
                **_receipt_payload(),
                "idempotency_key": "p1-89-historical-null-month",
            },
        )
        assert created.status_code == 201, created.text
        receipt_id = created.json()["id"]
        with session_factory() as session:
            receipt = session.get(ReturnReceipt, receipt_id)
            receipt.reconciliation_month = None
            session.commit()
        loaded = client.get(f"/api/finance/return_receipts/{receipt_id}")
        june = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-06"},
        )
        july = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-07"},
        )

    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["reconciliation_month"] is None
    assert loaded.json()["effective_reconciliation_month"] == "2026-06"
    assert loaded.json()["reconciliation_month_source"] == "historical_rule"
    assert june.json()["deliveries"][0]["delivery_id"] == 1
    assert july.json()["deliveries"] == []


def test_p1_89_month_adjustment_moves_only_candidate_membership_and_audits(
    finance_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import ReturnReceipt, ReturnReceiptItem
    from app.models.order import OrderItem

    app, session_factory = finance_api_app
    payload = _receipt_payload()
    payload["reconciliation_month"] = "2026-08"
    payload["idempotency_key"] = "p1-89-receipt-before-adjust"
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post("/api/finance/return_receipts", json=payload)
        assert created.status_code == 201, created.text
        before_august = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-08"},
        )
        adjusted = client.put(
            f"/api/finance/return_receipts/{created.json()['id']}/reconciliation-month",
            json={
                "reconciliation_month": "2026-09",
                "expected_version": created.json()["version"],
                "idempotency_key": "p1-89-adjust-to-september",
            },
        )
        replay = client.put(
            f"/api/finance/return_receipts/{created.json()['id']}/reconciliation-month",
            json={
                "reconciliation_month": "2026-09",
                "expected_version": created.json()["version"],
                "idempotency_key": "p1-89-adjust-to-september",
            },
        )
        after_august = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-08"},
        )
        after_september = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-09"},
        )

    assert before_august.json()["deliveries"][0]["delivery_id"] == 1
    assert adjusted.status_code == 200, adjusted.text
    assert replay.json() == adjusted.json()
    assert adjusted.json()["reconciliation_month"] == "2026-09"
    assert adjusted.json()["actual_received_date"] == "2026-06-14"
    assert adjusted.json()["version"] == 2
    assert after_august.json()["deliveries"] == []
    assert after_september.json()["deliveries"][0]["delivery_id"] == 1
    with session_factory() as session:
        receipt = session.get(ReturnReceipt, created.json()["id"])
        item = session.scalar(
            select(ReturnReceiptItem).where(
                ReturnReceiptItem.return_receipt_id == receipt.id
            )
        )
        assert receipt.actual_received_date == date(2026, 6, 14)
        assert item.actual_received_quantity == 78
        assert session.get(OrderItem, 1).delivered_quantity == 78
        audit = session.scalar(
            select(OperationLog)
            .where(OperationLog.action == "UPDATE_RETURN_RECONCILIATION_MONTH")
            .order_by(OperationLog.id.desc())
        )
        assert audit is not None
        assert '"reconciliation_month": "2026-08"' in audit.details
        assert '"reconciliation_month": "2026-09"' in audit.details


def test_p1_89_draft_statement_locks_direct_month_adjustment(
    finance_api_app,
) -> None:
    app, _ = finance_api_app
    payload = _receipt_payload()
    payload["idempotency_key"] = "p1-89-receipt-before-statement"
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post("/api/finance/return_receipts", json=payload)
        receipt_item_id = created.json()["items"][0]["id"]
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
                "idempotency_key": "p1-89-statement-create",
            },
        )
        replay = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
                "idempotency_key": "p1-89-statement-create",
            },
        )
        blocked = client.put(
            f"/api/finance/return_receipts/{created.json()['id']}/reconciliation-month",
            json={
                "reconciliation_month": "2026-07",
                "expected_version": created.json()["version"],
                "idempotency_key": "p1-89-adjust-after-draft",
            },
        )

    assert statement.status_code == 201, statement.text
    assert replay.json() == statement.json()
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "reconciliation_month_locked"
    assert "对账草稿" in blocked.json()["detail"]["message"]


def test_p1_89_period_adjustment_requires_dedicated_permission(
    finance_api_app,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        response = client.post(
            "/api/finance/return_receipts",
            json={
                **_receipt_payload(),
                "idempotency_key": "p1-89-sales-denied",
            },
        )
    assert response.status_code == 403


def test_short_receipt_allows_empty_optional_reason(finance_api_app) -> None:
    from app.models.finance import ReturnReceipt

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        response = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(" "),
        )

    assert response.status_code == 201, response.text
    assert response.json()["items"][0]["difference_reason"] is None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ReturnReceipt)) == 1


def test_create_receipt_78_of_80_and_reject_duplicate(finance_api_app) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        duplicate = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "confirmed"
    assert created.json()["items"][0]["delivered_quantity"] == 80
    assert created.json()["items"][0]["actual_received_quantity"] == 78
    assert created.json()["items"][0]["resolution_action"] == "continue_delivery"
    assert duplicate.status_code == 409
    with session_factory() as session:
        assert session.get(OrderItem, 1).delivered_quantity == 78
        assert session.get(OrderItem, 1).is_force_closed is False
        assert session.get(Order, 1).status == "partially_delivered"


def test_return_receipt_detail_can_be_loaded_for_editing(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        response = client.get(
            f"/api/finance/return_receipts/{created.json()['id']}"
        )

    assert response.status_code == 200
    assert response.json()["delivery_id"] == 1
    assert response.json()["items"][0]["actual_received_quantity"] == 78


def test_receipt_can_be_edited_before_statement_but_not_after(
    finance_api_app,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_id = created.json()["id"]
        receipt_item_id = created.json()["items"][0]["id"]
        edited = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "李经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 79,
                        "resolution_action": "continue_delivery",
                        "difference_reason": "压坏拒收1个",
                    }
                ],
            },
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )
        locked = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "李经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 80,
                        "difference_reason": None,
                    }
                ],
            },
        )

    assert edited.status_code == 200, edited.text
    assert edited.json()["items"][0]["actual_received_quantity"] == 79
    assert statement.status_code == 201, statement.text
    assert locked.status_code == 409


def test_confirmed_receipt_can_be_cancelled_and_reconfirmed(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_id = created.json()["id"]
        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")
        reopened = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "李经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 78,
                        "resolution_action": "continue_delivery",
                        "difference_reason": "压坏拒收 2 个",
                    }
                ],
            },
        )
        refreshed = client.get(f"/api/finance/return_receipts/{receipt_id}")

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert reopened.status_code == 200, reopened.text
    assert refreshed.json()["status"] == "confirmed"
    assert refreshed.json()["signed_by"] == "李经理"


def test_old_receipt_cannot_change_after_released_balance_is_dispatched(
    finance_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt
    from app.models.order import Order, OrderItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": 1,
                "actual_received_date": "2026-06-14",
                "reconciliation_month": "2026-06",
                "signed_by": "王经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 20,
                        "resolution_action": "continue_delivery",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        receipt_id = created.json()["id"]

        with session_factory() as session:
            receipt_created_at = session.get(ReturnReceipt, receipt_id).created_at
            later_delivery = Delivery(
                delivery_number="DH-20260615-002",
                customer_id=1,
                delivery_date=date(2026, 6, 15),
                status="dispatched",
                total_quantity=60,
                dispatched_at=receipt_created_at + timedelta(seconds=1),
            )
            session.add(later_delivery)
            session.flush()
            session.add(
                DeliveryItem(
                    delivery_id=later_delivery.id,
                    order_item_id=1,
                    delivered_quantity=60,
                    remarks=None,
                )
            )
            order_item = session.get(OrderItem, 1)
            order_item.delivered_quantity = 80
            session.get(Order, 1).status = "partially_delivered"
            session.commit()

        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")
        edited = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "王经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 20,
                        "resolution_action": "continue_delivery",
                    }
                ],
            },
        )

    assert cancelled.status_code == 409
    assert "DH-20260615-002" in cancelled.json()["detail"]
    assert edited.status_code == 409
    assert "DH-20260615-002" in edited.json()["detail"]
    with session_factory() as session:
        assert session.get(ReturnReceipt, receipt_id).status == "confirmed"
        assert session.get(OrderItem, 1).delivered_quantity == 80


def test_precreated_delivery_dispatched_after_receipt_blocks_old_receipt_change(
    finance_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt
    from app.models.order import Order, OrderItem

    app, session_factory = finance_api_app
    with session_factory() as session:
        queued_delivery = Delivery(
            delivery_number="DH-PRECREATED-LATER",
            customer_id=1,
            delivery_date=date(2026, 6, 15),
            status="pending",
            total_quantity=10,
        )
        session.add(queued_delivery)
        session.flush()
        queued_item = DeliveryItem(
            delivery_id=queued_delivery.id,
            order_item_id=1,
            delivered_quantity=10,
        )
        session.add(queued_item)
        session.flush()
        source_delivery = Delivery(
            delivery_number="DH-SOURCE-AFTER-QUEUE",
            customer_id=1,
            delivery_date=date(2026, 6, 14),
            status="dispatched",
            total_quantity=50,
            dispatched_at=datetime(2026, 6, 14, 9, 0, 0),
        )
        session.add(source_delivery)
        session.flush()
        source_item = DeliveryItem(
            delivery_id=source_delivery.id,
            order_item_id=1,
            delivered_quantity=50,
        )
        session.add(source_item)
        session.flush()
        assert queued_item.id < source_item.id
        session.get(OrderItem, 1).delivered_quantity = 130
        session.get(Order, 1).status = "delivered"
        queued_delivery_id = queued_delivery.id
        source_delivery_id = source_delivery.id
        source_item_id = source_item.id
        session.commit()

    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": source_delivery_id,
                "actual_received_date": "2026-06-14",
                "reconciliation_month": "2026-06",
                "items": [
                    {
                        "delivery_item_id": source_item_id,
                        "actual_received_quantity": 20,
                        "resolution_action": "continue_delivery",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        receipt_id = created.json()["id"]
        with session_factory() as session:
            receipt = session.get(ReturnReceipt, receipt_id)
            queued = session.get(Delivery, queued_delivery_id)
            queued.status = "dispatched"
            queued.dispatched_at = receipt.created_at + timedelta(seconds=1)
            session.get(OrderItem, 1).delivered_quantity = 110
            session.commit()

        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")

    assert cancelled.status_code == 409
    assert "DH-PRECREATED-LATER" in cancelled.json()["detail"]
    with session_factory() as session:
        assert session.get(ReturnReceipt, receipt_id).status == "confirmed"
        assert session.get(OrderItem, 1).delivered_quantity == 110


def test_accept_over_receipt_can_be_cancelled_after_unrelated_later_dispatch(
    finance_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt
    from app.models.order import OrderItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": 1,
                "actual_received_date": "2026-06-14",
                "reconciliation_month": "2026-06",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 81,
                        "resolution_action": "accept_over",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        receipt_id = created.json()["id"]
        with session_factory() as session:
            receipt = session.get(ReturnReceipt, receipt_id)
            later = Delivery(
                delivery_number="DH-AFTER-OVER-RECEIPT",
                customer_id=1,
                delivery_date=date(2026, 6, 15),
                status="dispatched",
                total_quantity=10,
                dispatched_at=receipt.created_at + timedelta(seconds=1),
            )
            session.add(later)
            session.flush()
            session.add(
                DeliveryItem(
                    delivery_id=later.id,
                    order_item_id=1,
                    delivered_quantity=10,
                )
            )
            session.get(OrderItem, 1).delivered_quantity = 91
            session.commit()

        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")

    assert cancelled.status_code == 200, cancelled.text
    with session_factory() as session:
        assert session.get(ReturnReceipt, receipt_id).status == "cancelled"
        assert session.get(OrderItem, 1).delivered_quantity == 90


def test_short_receipt_can_close_or_continue_and_over_receipt_is_allowed(
    finance_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        accepted_short = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": 1,
                "actual_received_date": "2026-06-14",
                "reconciliation_month": "2026-06",
                "signed_by": "王经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 20,
                        "resolution_action": "accept_short",
                    }
                ],
            },
        )
        assert accepted_short.status_code == 201, accepted_short.text
        with session_factory() as session:
            assert session.get(OrderItem, 1).delivered_quantity == 20
            assert session.get(OrderItem, 1).is_force_closed is True
            assert session.get(Order, 1).status == "delivered"
        cancelled = client.post(
            f"/api/finance/return_receipts/{accepted_short.json()['id']}/cancel"
        )
        assert cancelled.status_code == 200, cancelled.text
        with session_factory() as session:
            assert session.get(OrderItem, 1).delivered_quantity == 80
            assert session.get(OrderItem, 1).is_force_closed is False
        accepted_over = client.put(
            f"/api/finance/return_receipts/{accepted_short.json()['id']}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "王经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 105,
                        "resolution_action": "accept_over",
                        "difference_reason": "客户现场多收25只",
                    }
                ],
            },
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [accepted_over.json()["items"][0]["id"]],
            },
        )

    assert accepted_over.status_code == 200, accepted_over.text
    assert accepted_over.json()["items"][0]["resolution_action"] == "accept_over"
    assert accepted_over.json()["items"][0]["difference_reason"] == "客户现场多收25只"
    assert statement.status_code == 201, statement.text
    assert statement.json()["total_receivable"] == "378.00"
    with session_factory() as session:
        assert session.get(OrderItem, 1).delivered_quantity == 105
        assert session.get(OrderItem, 1).is_force_closed is False
        assert session.get(Order, 1).status == "delivered"
        assert session.get(DeliveryItem, 1).delivered_quantity == 80
        assert session.get(Delivery, 1).total_quantity == 80
        audit = session.scalar(
            select(OperationLog)
            .where(OperationLog.action == "UPDATE_RETURN_RECEIPT")
            .order_by(OperationLog.id.desc())
        )
        audit_details = loads(audit.details)
        assert audit_details["after"]["items"][0]["resolution_action"] == "accept_over"
        assert audit_details["after"]["items"][0]["difference_reason"] == "客户现场多收25只"


def test_receipt_20_of_100_reopens_remaining_80_for_delivery(
    finance_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem

    app, session_factory = finance_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).delivered_quantity = 100
        session.get(DeliveryItem, 1).delivered_quantity = 100
        session.get(Delivery, 1).total_quantity = 100
        session.commit()
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": 1,
                "actual_received_date": "2026-06-14",
                "reconciliation_month": "2026-06",
                "signed_by": "王经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 20,
                        "resolution_action": "continue_delivery",
                        "difference_reason": "客户先收20只，余80只后续补送",
                    }
                ],
            },
        )
        _login(client, "admin")
        pending = client.get("/api/deliveries/pending_items")

    assert receipt.status_code == 201, receipt.text
    assert pending.status_code == 200, pending.text
    row = next(
        item for item in pending.json()["items"] if item["order_item_id"] == 1
    )
    assert row["remaining_quantity"] == 80


def test_cancelled_receipt_item_cannot_create_statement(finance_api_app) -> None:
    from app.models.finance import Statement

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_item_id = created.json()["items"][0]["id"]
        cancelled = client.post(
            f"/api/finance/return_receipts/{created.json()['id']}/cancel"
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )

    assert cancelled.status_code == 200, cancelled.text
    assert statement.status_code == 409, statement.text
    assert "已取消回单" in statement.json()["detail"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Statement)) == 0


def test_concurrent_receipt_cancel_reverses_difference_only_once(
    finance_api_app,
) -> None:
    from app.models.finance import ReturnReceipt
    from app.models.order import OrderItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
    receipt_id = created.json()["id"]

    def cancel_request():
        with TestClient(app) as client:
            _login(client, "finance")
            return client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")

    responses = _race_requests(cancel_request, cancel_request)

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert "回单" in next(
        response.json()["detail"] for response in responses if response.status_code == 409
    )
    with session_factory() as session:
        assert session.get(ReturnReceipt, receipt_id).status == "cancelled"
        assert session.get(OrderItem, 1).delivered_quantity == 80


def test_cross_customer_resource_ids_are_forbidden(finance_api_app) -> None:
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    app, session_factory = finance_api_app
    with session_factory() as session:
        scoped_user = User(
            username="scoped-finance",
            password_hash=hash_password("RolePass123!"),
            role="finance",
            real_name="Scoped Finance",
            display_name="Scoped Finance",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_b = Customer(name="越权测试客户B")
        session.add_all([scoped_user, customer_b])
        session.flush()
        session.add(UserCustomerScope(user_id=scoped_user.id, customer_id=1))
        product = Product(
            customer_id=customer_b.id,
            product_code="CROSS-B-001",
            customer_material_code="CROSS-B-MAT",
            product_name="跨客户纸箱",
            legacy_material_text="K=A",
            box_category="normal",
            cost_unit_price=Decimal("2.00"),
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="CROSS-B-ORDER",
            customer_id=customer_b.id,
            customer_po="CROSS-B-PO",
            order_date=date(2026, 6, 1),
            delivery_date=date(2026, 6, 13),
            status="delivered",
            payment_status="unpaid",
            total_amount=Decimal("30.00"),
        )
        session.add(order)
        session.flush()
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=10,
            delivered_quantity=10,
            unit_price=Decimal("3.00"),
            subtotal=Decimal("30.00"),
            material_status="received",
            snapshot_product_name="跨客户纸箱",
            snapshot_spec="300×200×100mm",
            snapshot_material="K=A",
        )
        session.add(order_item)
        session.flush()
        delivery = Delivery(
            delivery_number="CROSS-B-DELIVERY",
            customer_id=customer_b.id,
            delivery_date=date(2026, 6, 13),
            status="dispatched",
            total_quantity=10,
        )
        session.add(delivery)
        session.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=order_item.id,
            delivered_quantity=10,
        )
        session.add(delivery_item)
        session.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 6, 14),
            signed_by="客户B",
            status="confirmed",
        )
        session.add(receipt)
        session.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=10,
        )
        session.add(receipt_item)
        session.commit()
        delivery_id = delivery.id
        delivery_item_id = delivery_item.id
        receipt_id = receipt.id
        receipt_item_id = receipt_item.id

    with TestClient(app) as client:
        _login(client, "scoped-finance")
        created = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": delivery_id,
                "actual_received_date": "2026-06-14",
                "reconciliation_month": "2026-06",
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 10,
                    }
                ],
            },
        )
        loaded = client.get(f"/api/finance/return_receipts/{receipt_id}")
        updated = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 10,
                    }
                ],
            },
        )
        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )

    assert [
        created.status_code,
        loaded.status_code,
        updated.status_code,
        cancelled.status_code,
        statement.status_code,
    ] == [403, 403, 403, 403, 403]


def test_cancelled_receipt_is_blocked_when_statement_exists(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_id = created.json()["id"]
        receipt_item_id = created.json()["items"][0]["id"]
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )
        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")

    assert statement.status_code == 201, statement.text
    assert cancelled.status_code == 409
    assert "对账" in cancelled.json()["detail"]


def test_statement_detail_endpoint_returns_items_and_summary(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        response = client.get(f"/api/finance/statements/{statement['id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["statement_number"] == statement["statement_number"]
    assert body["customer_name"] == "苏州思迈尔包装有限公司"
    assert body["status"] == "unsettled"
    assert body["items"][0]["receivable_amount"] == "280.80"


def test_statement_customer_options_cover_eligible_customers(finance_api_app) -> None:
    app, session_factory = finance_api_app
    with session_factory() as session:
        from app.models.customer import Customer
        from app.models.delivery import Delivery, DeliveryItem
        from app.models.order import Order, OrderItem
        from app.models.product import Product

        customer = Customer(
            customer_number=2,
            customer_code="HT",
            name="昆山宏泰包装有限公司",
            payment_term_days=30,
        )
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="HT-001",
            customer_material_code="HT-001",
            product_name="五层纸箱",
            legacy_material_text="K=A",
            box_category="normal",
            cost_unit_price=Decimal("1.20"),
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260613-002",
            customer_id=customer.id,
            customer_po="HT-PO-001",
            order_date=date(2026, 6, 2),
            delivery_date=date(2026, 6, 14),
            status="partially_delivered",
            payment_status="unpaid",
            total_amount=Decimal("120"),
        )
        session.add(order)
        session.flush()
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=50,
            delivered_quantity=50,
            unit_price=Decimal("2.40"),
            subtotal=Decimal("120"),
            material_status="received",
            snapshot_product_name="五层纸箱",
            snapshot_spec="520×350×300mm",
            snapshot_material="K=A",
        )
        session.add(order_item)
        session.flush()
        delivery = Delivery(
            delivery_number="DH-20260614-002",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 14),
            vehicle_number="苏E·54321",
            status="dispatched",
            total_quantity=50,
            dispatched_at=datetime(2026, 6, 14, 10, 0, 0),
        )
        session.add(delivery)
        session.flush()
        session.add(
            DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=50,
                remarks="第二批",
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "finance")
        first_receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": 2,
                "actual_received_date": "2026-06-15",
                "reconciliation_month": "2026-06",
                "signed_by": "王经理",
                "items": [
                    {
                        "delivery_item_id": 2,
                        "actual_received_quantity": 50,
                        "difference_reason": None,
                    }
                ],
            },
        )
        response = client.get("/api/finance/statement-customers", params={"statement_month": "2026-06"})

    assert first_receipt.status_code == 201, first_receipt.text
    assert receipt.status_code == 201, receipt.text
    assert response.status_code == 200, response.text
    customer_names = {row["name"] for row in response.json()["items"]}
    assert "苏州思迈尔包装有限公司" in customer_names
    assert "昆山宏泰包装有限公司" in customer_names


def test_statement_edit_and_cancel_endpoints_exist_for_frontend_controls(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        edited = client.put(
            f"/api/finance/statements/{statement['id']}",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
            },
        )
        cancelled = client.post(f"/api/finance/statements/{statement['id']}/cancel")

    assert edited.status_code == 200, edited.text
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.parametrize("role", ["sales", "workshop"])
def test_finance_routes_reject_non_finance_roles(
    finance_api_app,
    role: str,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, role)
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        pending = client.get(
            "/api/finance/pending_statements?customer_id=1"
        )

    assert receipt.status_code == 403
    assert pending.status_code == 403


def test_pending_statement_and_statement_snapshot_amounts(
    finance_api_app,
) -> None:
    from app.models.finance import Statement, StatementItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_item_id = receipt.json()["items"][0]["id"]
        pending = client.get(
            "/api/finance/pending_statements?customer_id=1"
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )
        pending_after = client.get(
            "/api/finance/pending_statements?customer_id=1"
        )
        duplicate = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )

    assert pending.status_code == 200
    assert Decimal(
        str(pending.json()["items"][0]["receivable_amount"])
    ) == Decimal("280.80")
    assert statement.status_code == 201, statement.text
    assert Decimal(str(statement.json()["total_receivable"])) == Decimal("280.80")
    assert Decimal(str(statement.json()["total_gross_profit"])) == Decimal("70.20")
    assert pending_after.json()["items"] == []
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["id"] == statement.json()["id"]
    with session_factory() as session:
        master = session.scalar(select(Statement))
        line = session.scalar(select(StatementItem))
        assert len(session.scalars(select(Statement)).all()) == 1
        assert len(session.scalars(select(StatementItem)).all()) == 1
    assert master.total_receivable == Decimal("280.80")
    assert line.unit_price_snapshot == Decimal("3.6000")
    assert line.unit_cost_snapshot == Decimal("2.7000")
    assert line.receivable_amount == Decimal("280.80")
    assert line.gross_profit_amount == Decimal("70.20")


def test_finance_api_hides_legacy_history_prefix_in_pending_statement(
    finance_api_app,
) -> None:
    from app.models.order import Order

    app, session_factory = finance_api_app
    with session_factory() as session:
        session.get(Order, 1).order_number = "RUIDA-42001"
        session.commit()
    with TestClient(app) as client:
        _login(client, "finance")
        client.post("/api/finance/return_receipts", json=_receipt_payload())
        pending = client.get("/api/finance/pending_statements?customer_id=1")

    assert pending.status_code == 200
    first = pending.json()["items"][0]
    assert first["display_order_number"] == "TM20260601-0001"
    assert first["order_number"] == "TM20260601-0001"
    assert "RUIDA" not in str(pending.json())


def test_phase8_migration_preserves_legacy_finance_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE return_confirmations (
                id INTEGER PRIMARY KEY,
                delivery_id INTEGER NOT NULL
            );
            CREATE TABLE statements (
                id INTEGER PRIMARY KEY,
                statement_no TEXT NOT NULL
            );
            INSERT INTO return_confirmations VALUES (3, 7);
            INSERT INTO statements VALUES (4, 'ST20260500001');
            """
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase8-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        receipt = connection.execute(
            "SELECT id, delivery_id FROM return_confirmations"
        ).fetchone()
        statement = connection.execute(
            "SELECT id, statement_no FROM statements"
        ).fetchone()

    assert {
        "finance_return_receipts",
        "finance_return_receipt_items",
        "finance_statements",
        "finance_statement_items",
    } <= tables
    assert receipt == (3, 7)
    assert statement == (4, "ST20260500001")


def _create_statement(client: TestClient) -> dict:
    receipt = client.post(
        "/api/finance/return_receipts",
        json=_receipt_payload(),
    )
    assert receipt.status_code == 201, receipt.text
    receipt_item_id = receipt.json()["items"][0]["id"]
    statement = client.post(
        "/api/finance/statements",
        json={
            "customer_id": 1,
            "statement_month": "2026-06",
            "return_receipt_item_ids": [receipt_item_id],
        },
    )
    assert statement.status_code == 201, statement.text
    return statement.json()


def test_invoice_and_partial_settlement_are_cumulative_and_audited(
    finance_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import Invoice, SettlementRecord, Statement

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        first_invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": statement["id"],
                "invoice_number": "INV-202606-001",
                "invoice_date": "2026-06-14",
                "invoice_amount": "180.80",
            },
        )
        second_invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": statement["id"],
                "invoice_number": "INV-202606-002",
                "invoice_date": "2026-06-14",
                "invoice_amount": "100.00",
            },
        )
        partial_payment = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "80.80",
                "settlement_date": "2026-06-14",
                "account": "中国银行 6688",
            },
        )

    assert first_invoice.status_code == 201, first_invoice.text
    assert second_invoice.status_code == 201, second_invoice.text
    assert partial_payment.status_code == 200, partial_payment.text
    assert Decimal(str(second_invoice.json()["invoiced_amount"])) == Decimal("280.80")
    assert Decimal(str(partial_payment.json()["settled_amount"])) == Decimal("80.80")
    assert partial_payment.json()["status"] == "unsettled"
    with session_factory() as session:
        master = session.get(Statement, statement["id"])
        assert master.invoiced_amount == Decimal("280.80")
        assert master.settled_amount == Decimal("80.80")
        assert session.scalar(select(func.count()).select_from(Invoice)) == 2
        assert session.scalar(select(func.count()).select_from(SettlementRecord)) == 1
        actions = set(session.scalars(select(OperationLog.action)).all())
        assert {"REGISTER_INVOICE", "SETTLE_STATEMENT"} <= actions


def test_exact_settlement_closes_statement_and_rejects_overpayment(
    finance_api_app,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        statement = _create_statement(client)
        settled = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "280.80",
                "settlement_date": "2026-06-14",
                "account": "现金",
            },
        )
        overpayment = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "0.01",
                "settlement_date": "2026-06-14",
                "account": "现金",
            },
        )

    assert settled.status_code == 200, settled.text
    assert settled.json()["status"] == "settled"
    assert settled.json()["status_label"] == "已结清"
    assert overpayment.status_code == 409


def test_settlement_accepts_missing_or_blank_account(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        statement = _create_statement(client)
        missing_account = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "100.00",
                "settlement_date": "2026-06-14",
            },
        )
        blank_account = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "100.00",
                "settlement_date": "2026-06-15",
                "account": "",
            },
        )

    assert missing_account.status_code == 200, missing_account.text
    assert blank_account.status_code == 200, blank_account.text


@pytest.mark.parametrize("role", ["sales", "workshop"])
def test_invoice_and_settlement_reject_non_finance_roles(
    finance_api_app,
    role: str,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, role)
        invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": 1,
                "invoice_number": "NO-ACCESS",
                "invoice_date": "2026-06-14",
                "invoice_amount": "1.00",
            },
        )
        settlement = client.put(
            "/api/finance/statements/1/settle",
            json={
                "amount": "1.00",
                "settlement_date": "2026-06-14",
                "account": "NO-ACCESS",
            },
        )

    assert invoice.status_code == 403
    assert settlement.status_code == 403


def test_finance_lists_statements_and_invoice_records(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": statement["id"],
                "invoice_number": "INV-LIST-001",
                "invoice_date": "2026-06-14",
                "invoice_amount": "100.00",
            },
        )
        assert invoice.status_code == 201, invoice.text
        statements = client.get("/api/finance/statements")
        invoices = client.get("/api/finance/invoices")

    assert statements.status_code == 200, statements.text
    assert statements.json()["items"][0]["statement_number"].startswith("ST-")
    assert statements.json()["items"][0]["customer_name"]
    assert invoices.status_code == 200, invoices.text
    assert invoices.json()["items"][0]["invoice_number"] == "INV-LIST-001"


def test_current_finance_groups_customer_month_and_uses_real_balances(
    finance_api_app,
) -> None:
    from app.models.customer import Customer
    from app.models.finance import Statement

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        first = _create_statement(client)
        with session_factory() as session:
            first_statement = session.get(Statement, first["id"])
            first_statement.invoiced_amount = Decimal("100.00")
            first_statement.settled_amount = Decimal("80.00")
            first_statement.status = "unsettled"
            session.add(
                Statement(
                    statement_number="ST-202606-0002",
                    customer_id=1,
                    statement_month="2026-06",
                    total_receivable=Decimal("100.00"),
                    total_gross_profit=Decimal("0.00"),
                    invoiced_amount=Decimal("40.00"),
                    settled_amount=Decimal("20.00"),
                    status="settled",
                    created_by=1,
                )
            )
            settled_customer = Customer(
                customer_number=2,
                customer_code="DONE",
                name="已结清客户",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
            session.add(settled_customer)
            session.flush()
            session.add(
                Statement(
                    statement_number="ST-202606-DONE",
                    customer_id=settled_customer.id,
                    statement_month="2026-06",
                    total_receivable=Decimal("50.00"),
                    total_gross_profit=Decimal("0.00"),
                    invoiced_amount=Decimal("50.00"),
                    settled_amount=Decimal("50.00"),
                    status="unsettled",
                    created_by=1,
                )
            )
            session.commit()
        response = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-06"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["summary"]["customer_count"] == 1
    row = body["items"][0]
    assert row["customer_id"] == 1
    assert row["statement_count"] == 2
    assert len(row["statements"]) == 2
    assert Decimal(str(row["reconciled_receivable_amount"])) == Decimal("380.80")
    assert Decimal(str(row["pending_invoice_amount"])) == Decimal("240.80")
    assert Decimal(str(row["pending_payment_amount"])) == Decimal("280.80")
    assert row["primary_action"] == "invoice"
    assert row["status_anomaly_count"] == 1
    assert "total_gross_profit" not in row
    assert "total_gross_profit" not in row["statements"][0]


def test_current_finance_prioritizes_pending_reconciliation(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        response = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-06"},
        )
        forbidden = client.post("/api/auth/login", json={
            "username": "workshop",
            "password": "RolePass123!",
        })
        assert forbidden.status_code == 200
        denied = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-06"},
        )

    assert receipt.status_code == 201, receipt.text
    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    assert row["primary_action"] == "reconcile"
    assert row["pending_reconciliation_count"] == 1
    assert Decimal(str(row["pending_reconciliation_amount"])) == Decimal("280.80")
    assert row["statements"] == []
    assert denied.status_code == 403


def _fully_process_statement(
    client: TestClient,
    statement: dict,
    *,
    invoice_number: str,
    transaction_date: str,
) -> None:
    amount = str(statement["total_receivable"])
    invoice = client.post(
        "/api/finance/invoices",
        json={
            "statement_id": statement["id"],
            "invoice_number": invoice_number,
            "invoice_date": transaction_date,
            "invoice_amount": amount,
        },
    )
    settlement = client.put(
        f"/api/finance/statements/{statement['id']}/settle",
        json={
            "amount": amount,
            "settlement_date": transaction_date,
            "account": "测试账户",
        },
    )
    assert invoice.status_code == 201, invoice.text
    assert settlement.status_code == 200, settlement.text


def test_settled_history_groups_full_customer_month_and_supports_search(
    finance_api_app,
) -> None:
    from app.models.finance import Invoice, SettlementRecord, Statement

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        first = _create_statement(client)
        _fully_process_statement(
            client,
            first,
            invoice_number="INV-HIST-001",
            transaction_date="2026-06-18",
        )
        with session_factory() as session:
            second = Statement(
                statement_number="ST-202606-HIST-002",
                customer_id=1,
                statement_month="2026-06",
                total_receivable=Decimal("100.00"),
                total_gross_profit=Decimal("0.00"),
                invoiced_amount=Decimal("100.00"),
                settled_amount=Decimal("100.00"),
                status="unsettled",
                created_by=1,
            )
            session.add(second)
            session.flush()
            session.add_all(
                [
                    Invoice(
                        statement_id=second.id,
                        invoice_number="INV-HIST-002",
                        invoice_date=date(2026, 6, 20),
                        invoice_amount=Decimal("100.00"),
                        created_by=1,
                    ),
                    SettlementRecord(
                        statement_id=second.id,
                        settled_amount=Decimal("100.00"),
                        settlement_date=date(2026, 6, 20),
                        account="测试账户",
                        created_by=1,
                    ),
                ]
            )
            session.commit()

        response = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026},
        )
        by_statement = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026, "statement_number": "HIST-002"},
        )
        by_invoice = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026, "invoice_number": "HIST-002"},
        )
        by_settlement_date = client.get(
            "/api/finance/settled-customer-months",
            params={
                "year": 2026,
                "settlement_date_from": "2026-06-20",
                "settlement_date_to": "2026-06-20",
            },
        )
        no_match = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026, "invoice_number": "不存在"},
        )
        bad_month = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2025, "statement_month": "2026-06"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["summary"]["customer_month_count"] == 1
    assert body["summary"]["statement_count"] == 2
    row = body["items"][0]
    assert row["statement_month"] == "2026-06"
    assert row["customer_id"] == 1
    assert row["statement_count"] == 2
    assert len(row["statements"]) == 2
    assert Decimal(str(row["total_receivable"])) == Decimal("380.80")
    assert Decimal(str(row["invoiced_amount"])) == Decimal("380.80")
    assert Decimal(str(row["settled_amount"])) == Decimal("380.80")
    assert row["status_anomaly_count"] == 1
    assert set(row["invoice_numbers"]) == {"INV-HIST-001", "INV-HIST-002"}
    assert row["latest_settlement_date"] == "2026-06-20"
    assert "total_gross_profit" not in row
    assert "total_gross_profit" not in row["statements"][0]
    assert by_statement.json()["total"] == 1
    assert by_invoice.json()["total"] == 1
    assert by_settlement_date.json()["total"] == 1
    assert no_match.json()["total"] == 0
    assert bad_month.status_code == 400


def test_settled_history_excludes_customer_month_with_any_balance(
    finance_api_app,
) -> None:
    from app.models.finance import Statement

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        first = _create_statement(client)
        _fully_process_statement(
            client,
            first,
            invoice_number="INV-BALANCE-001",
            transaction_date="2026-06-18",
        )
        with session_factory() as session:
            second = Statement(
                statement_number="ST-202606-BALANCE-002",
                customer_id=1,
                statement_month="2026-06",
                total_receivable=Decimal("100.00"),
                total_gross_profit=Decimal("0.00"),
                invoiced_amount=Decimal("90.00"),
                settled_amount=Decimal("100.00"),
                status="settled",
                created_by=1,
            )
            session.add(second)
            session.commit()

        history_before = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026},
        )
        current_before = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-06"},
        )
        with session_factory() as session:
            second = session.scalar(
                select(Statement).where(
                    Statement.statement_number == "ST-202606-BALANCE-002"
                )
            )
            second.invoiced_amount = Decimal("100.00")
            second.settled_amount = Decimal("100.00")
            second.status = "settled"
            session.commit()
        history_after = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026},
        )

    assert history_before.status_code == 200
    assert history_before.json()["total"] == 0
    assert current_before.status_code == 200
    assert current_before.json()["total"] == 1
    assert current_before.json()["items"][0]["primary_action"] == "invoice"
    assert history_after.status_code == 200
    assert history_after.json()["total"] == 1


def test_settled_history_excludes_month_with_new_pending_reconciliation(
    finance_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        first = _create_statement(client)
        _fully_process_statement(
            client,
            first,
            invoice_number="INV-PENDING-001",
            transaction_date="2026-06-18",
        )
        with session_factory() as session:
            delivery = Delivery(
                delivery_number="DH-20260620-PENDING",
                customer_id=1,
                delivery_date=date(2026, 6, 19),
                status="dispatched",
                total_quantity=1,
                dispatched_at=datetime(2026, 6, 19, 9, 0, 0),
            )
            session.add(delivery)
            session.flush()
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=1,
                delivered_quantity=1,
            )
            session.add(delivery_item)
            session.flush()
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=date(2026, 6, 19),
                status="confirmed",
                created_by=1,
            )
            session.add(receipt)
            session.flush()
            session.add(
                ReturnReceiptItem(
                    return_receipt_id=receipt.id,
                    delivery_item_id=delivery_item.id,
                    actual_received_quantity=1,
                )
            )
            session.commit()

        history = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026},
        )
        current = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-06"},
        )

    assert history.status_code == 200, history.text
    assert history.json()["total"] == 0
    assert current.status_code == 200, current.text
    assert current.json()["items"][0]["primary_action"] == "reconcile"


def test_settled_history_requires_finance_view_permission(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "workshop")
        denied = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026},
        )

    assert denied.status_code == 403


def _append_second_delivery_line(session_factory) -> int:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem

    with session_factory() as session:
        second_order_item = OrderItem(
            order_id=1,
            product_id=1,
            quantity=20,
            delivered_quantity=20,
            unit_price=Decimal("4.20"),
            subtotal=Decimal("84.00"),
            material_status="received",
            snapshot_product_name="五层加强纸箱-第二规格",
            snapshot_spec="420×300×250mm",
            snapshot_material="K=A-BC",
        )
        session.add(second_order_item)
        session.flush()
        delivery = session.get(Delivery, 1)
        delivery.total_quantity = 100
        second_delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=second_order_item.id,
            delivered_quantity=20,
            remarks="同一张送货单第二行",
        )
        session.add(second_delivery_item)
        session.commit()
        return second_delivery_item.id


def _two_line_receipt_payload(second_delivery_item_id: int) -> dict:
    return {
        "delivery_id": 1,
        "actual_received_date": "2026-06-14",
        "reconciliation_month": "2026-06",
        "signed_by": "王经理",
        "items": [
            {
                "delivery_item_id": 1,
                "actual_received_quantity": 80,
                "difference_reason": None,
            },
            {
                "delivery_item_id": second_delivery_item_id,
                "actual_received_quantity": 20,
                "difference_reason": None,
            },
        ],
    }


def test_statement_can_select_individual_receipt_lines(
    finance_api_app,
) -> None:
    from app.models.finance import StatementItem

    app, session_factory = finance_api_app
    second_delivery_item_id = _append_second_delivery_line(session_factory)
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_two_line_receipt_payload(second_delivery_item_id),
        )
        assert receipt.status_code == 201, receipt.text
        receipt_item_ids = [item["id"] for item in receipt.json()["items"]]
        pending = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-06"},
        )
        customers = client.get(
            "/api/finance/statement-customers",
            params={"statement_month": "2026-06"},
        )
        partial = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_ids[0]],
            },
        )

    assert pending.status_code == 200, pending.text
    delivery = pending.json()["deliveries"][0]
    assert delivery["delivery_id"] == 1
    assert delivery["item_count"] == 2
    assert delivery["selection_blocked"] is False
    assert len(delivery["items"]) == 2
    assert customers.json()["items"][0]["pending_count"] == 1
    assert partial.status_code == 201, partial.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(StatementItem)) == 1


@pytest.mark.parametrize(
    ("delivery_date", "included_month", "excluded_month"),
    [
        (date(2026, 7, 19), "2026-07", "2026-08"),
        (date(2026, 7, 20), "2026-08", "2026-07"),
    ],
)
def test_statement_period_uses_delivery_date_and_customer_cutoff(
    finance_api_app,
    delivery_date: date,
    included_month: str,
    excluded_month: str,
) -> None:
    from app.models.delivery import Delivery

    app, session_factory = finance_api_app
    with session_factory() as session:
        session.get(Delivery, 1).delivery_date = delivery_date
        session.commit()
    payload = _receipt_payload(None)
    payload["actual_received_date"] = "2026-07-25"
    payload["reconciliation_month"] = included_month
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post("/api/finance/return_receipts", json=payload)
        included = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": included_month},
        )
        excluded = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": excluded_month},
        )

    assert receipt.status_code == 201, receipt.text
    assert [row["delivery_id"] for row in included.json()["deliveries"]] == [1]
    assert excluded.json()["deliveries"] == []


def test_partially_reconciled_delivery_keeps_remaining_line_selectable(
    finance_api_app,
) -> None:
    from app.models.finance import Statement, StatementItem

    app, session_factory = finance_api_app
    second_delivery_item_id = _append_second_delivery_line(session_factory)
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_two_line_receipt_payload(second_delivery_item_id),
        )
        assert receipt.status_code == 201, receipt.text
        first_receipt_item_id = receipt.json()["items"][0]["id"]
        with session_factory() as session:
            old_statement = Statement(
                statement_number="ST-202606-0099",
                customer_id=1,
                statement_month="2026-06",
                total_receivable=Decimal("288.00"),
                total_gross_profit=Decimal("72.00"),
                status="unsettled",
                created_by=1,
            )
            session.add(old_statement)
            session.flush()
            session.add(
                StatementItem(
                    statement_id=old_statement.id,
                    return_receipt_item_id=first_receipt_item_id,
                    actual_received_quantity=80,
                    unit_price_snapshot=Decimal("3.60"),
                    unit_cost_snapshot=Decimal("2.70"),
                    receivable_amount=Decimal("288.00"),
                    gross_profit_amount=Decimal("72.00"),
                )
            )
            session.commit()
        pending = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-06"},
        )
        remaining_item_id = receipt.json()["items"][1]["id"]
        create = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [remaining_item_id],
            },
        )

    remaining = pending.json()["deliveries"][0]
    assert remaining["selection_blocked"] is False
    assert remaining["pending_item_count"] == 1
    assert remaining["exception_reason"] is None
    assert [row["return_receipt_item_id"] for row in pending.json()["items"]] == [remaining_item_id]
    assert create.status_code == 201, create.text


def test_monthly_yearly_report_uses_same_customer_month_totals_and_aging(
    finance_api_app,
) -> None:
    from app.models.customer import Customer
    from app.models.finance import Statement

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        june_statement = _create_statement(client)
        with session_factory() as session:
            second_customer = Customer(
                customer_number=2,
                customer_code="REPORT-2",
                name="报表第二客户",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
            session.add(second_customer)
            session.flush()
            session.add_all(
                [
                    Statement(
                        statement_number="ST-202607-REPORT-1",
                        customer_id=1,
                        statement_month="2026-07",
                        total_receivable=Decimal("100.00"),
                        total_gross_profit=Decimal("999.00"),
                        invoiced_amount=Decimal("60.00"),
                        settled_amount=Decimal("40.00"),
                        status="unsettled",
                        created_by=1,
                    ),
                    Statement(
                        statement_number="ST-202607-REPORT-2",
                        customer_id=second_customer.id,
                        statement_month="2026-07",
                        total_receivable=Decimal("50.00"),
                        total_gross_profit=Decimal("888.00"),
                        invoiced_amount=Decimal("50.00"),
                        settled_amount=Decimal("50.00"),
                        status="unsettled",
                        created_by=1,
                    ),
                    Statement(
                        statement_number="ST-202605-REPORT-1",
                        customer_id=1,
                        statement_month="2026-05",
                        total_receivable=Decimal("90.00"),
                        total_gross_profit=Decimal("777.00"),
                        invoiced_amount=Decimal("0.00"),
                        settled_amount=Decimal("0.00"),
                        status="unsettled",
                        created_by=1,
                    ),
                    Statement(
                        statement_number="ST-202603-REPORT-2",
                        customer_id=second_customer.id,
                        statement_month="2026-03",
                        total_receivable=Decimal("300.00"),
                        total_gross_profit=Decimal("666.00"),
                        invoiced_amount=Decimal("300.00"),
                        settled_amount=Decimal("100.00"),
                        status="settled",
                        created_by=1,
                    ),
                ]
            )
            session.commit()

        response = client.get(
            "/api/finance/reports/monthly-yearly",
            params={"year": 2026, "statement_month": "2026-07"},
        )
        only_second = client.get(
            "/api/finance/reports/monthly-yearly",
            params={
                "year": 2026,
                "statement_month": "2026-07",
                "customer_id": 2,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_month"] == "2026-07"
    assert body["included_through_statement_month"] == "2026-07"
    assert body["balance_basis"] == "current_balance_by_statement_month"
    assert len(body["monthly"]) == 12
    july = next(row for row in body["monthly"] if row["statement_month"] == "2026-07")
    assert Decimal(str(july["reconciled_receivable_amount"])) == Decimal("150.00")
    assert Decimal(str(july["invoiced_amount"])) == Decimal("110.00")
    assert Decimal(str(july["settled_amount"])) == Decimal("90.00")
    assert Decimal(str(july["pending_payment_amount"])) == Decimal("60.00")
    assert july["reconciled_customer_count"] == 2
    assert july["pending_payment_customer_count"] == 1

    detail = body["selected_month_customers"]
    assert len(detail) == 2
    assert sum(
        (Decimal(str(row["reconciled_receivable_amount"])) for row in detail),
        Decimal("0.00"),
    ) == Decimal(str(july["reconciled_receivable_amount"]))
    assert sum(
        (Decimal(str(row["pending_payment_amount"])) for row in detail),
        Decimal("0.00"),
    ) == Decimal(str(july["pending_payment_amount"]))
    assert all("total_gross_profit" not in row for row in detail)

    balances = {row["customer_id"]: row for row in body["customer_balances"]}
    assert Decimal(str(balances[1]["outstanding_amount"])) == Decimal("430.80")
    assert Decimal(str(balances[2]["outstanding_amount"])) == Decimal("200.00")
    aging = {row["key"]: row for row in body["aging"]}
    assert Decimal(str(aging["current"]["outstanding_amount"])) == Decimal("60.00")
    assert Decimal(str(aging["one_month"]["outstanding_amount"])) == Decimal("280.80")
    assert Decimal(str(aging["two_months"]["outstanding_amount"])) == Decimal("90.00")
    assert Decimal(str(aging["three_plus"]["outstanding_amount"])) == Decimal("200.00")
    assert only_second.status_code == 200, only_second.text
    assert {row["customer_id"] for row in only_second.json()["selected_month_customers"]} == {2}
    assert june_statement["id"] > 0


def test_monthly_yearly_report_keeps_unreconciled_receipts_out_of_receivables(
    finance_api_app,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        response = client.get(
            "/api/finance/reports/monthly-yearly",
            params={"year": 2026, "statement_month": "2026-06"},
        )
        bad_month = client.get(
            "/api/finance/reports/monthly-yearly",
            params={"year": 2025, "statement_month": "2026-06"},
        )
        denied_login = client.post(
            "/api/auth/login",
            json={"username": "workshop", "password": "RolePass123!"},
        )
        assert denied_login.status_code == 200
        denied = client.get(
            "/api/finance/reports/monthly-yearly",
            params={"year": 2026, "statement_month": "2026-06"},
        )

    assert receipt.status_code == 201, receipt.text
    assert response.status_code == 200, response.text
    june = response.json()["selected_month_summary"]
    assert Decimal(str(june["pending_reconciliation_amount"])) == Decimal("280.80")
    assert june["pending_reconciliation_customer_count"] == 1
    assert Decimal(str(june["reconciled_receivable_amount"])) == Decimal("0.00")
    assert response.json()["customer_balances"] == []
    assert response.json()["yearly_summary"]["record_count"] == 1
    assert bad_month.status_code == 400
    assert denied.status_code == 403

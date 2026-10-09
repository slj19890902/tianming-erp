from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_phase8_finance import _login, _receipt_payload, finance_api_app


def _historical_delivery_payload(**overrides) -> dict:
    payload = {
        "customer_id": 1,
        "delivery_date": "2026-06-10",
        "historical_backfill": True,
        "idempotency_key": "p0-31-historical-create-001",
        "source_mode": "order",
        "items": [
            {
                "source_type": "order",
                "order_item_id": 1,
                "delivered_quantity": 10,
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_p0_31_historical_create_preserves_erp_time_and_replays(
    finance_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.delivery import Delivery
    from app.models.finance import FinanceIdempotencyRecord

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        normal_backdate = client.post(
            "/api/deliveries",
            json={
                **_historical_delivery_payload(),
                "historical_backfill": False,
                "idempotency_key": None,
            },
        )
        before_order = client.post(
            "/api/deliveries",
            json={
                **_historical_delivery_payload(),
                "delivery_date": "2026-05-31",
                "idempotency_key": "p0-31-before-order-date",
            },
        )
        future = client.post(
            "/api/deliveries",
            json={
                **_historical_delivery_payload(),
                "delivery_date": "2099-01-01",
                "idempotency_key": "p0-31-future-date-001",
            },
        )
        created = client.post("/api/deliveries", json=_historical_delivery_payload())
        replay = client.post("/api/deliveries", json=_historical_delivery_payload())
        changed = client.post(
            "/api/deliveries",
            json={
                **_historical_delivery_payload(),
                "delivery_date": "2026-06-11",
            },
        )

    assert normal_backdate.status_code == 400
    assert "补录历史送货" in normal_backdate.text
    assert before_order.status_code == 400
    assert "2026-06-01" in before_order.text
    assert future.status_code == 400
    assert created.status_code == 201, created.text
    assert replay.status_code == 201, replay.text
    assert replay.json() == created.json()
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "delivery_idempotency_conflict"
    assert created.json()["delivery_date"] == "2026-06-10"
    assert created.json()["is_historical_backfill"] is True
    assert created.json()["backfilled_by"] is not None
    assert created.json()["created_at"] != "2026-06-10"
    assert created.json()["suggested_reconciliation_month"] == "2026-06"
    with session_factory() as session:
        row = session.get(Delivery, created.json()["id"])
        assert row.created_at.date() != date(2026, 6, 10)
        assert row.backfilled_at is not None
        assert session.scalar(
            select(FinanceIdempotencyRecord).where(
                FinanceIdempotencyRecord.resource_type == "delivery",
                FinanceIdempotencyRecord.resource_id == row.id,
            )
        ) is not None
        audit = session.scalar(
            select(OperationLog)
            .where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == row.id,
            )
            .order_by(OperationLog.id.desc())
        )
        assert audit is not None
        assert "is_historical_backfill" in audit.details


@pytest.mark.parametrize("delivery_day,explicit_month,expected_month", [
    (date(2026, 6, 13), None, "2026-06"),
    (date(2026, 6, 13), "2026-08", "2026-08"),
    (date(2026, 8, 19), None, "2026-08"),
    (date(2026, 8, 20), None, "2026-09"),
])
def test_p1_89_delivery_cycle_default_and_legacy_null_compatibility(
    finance_api_app,
    monkeypatch,
    delivery_day,
    explicit_month,
    expected_month,
) -> None:
    from app.api import finance as finance_api
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.finance import ReturnReceipt

    app, session_factory = finance_api_app
    with session_factory() as session:
        session.get(Customer, 1).statement_cycle_start_day = 20
        session.get(Delivery, 1).delivery_date = delivery_day
        session.commit()
    monkeypatch.setattr(finance_api, "beijing_today", lambda: date(2026, 8, 28))
    modern_payload = _receipt_payload()
    modern_payload.pop("reconciliation_month", None)
    modern_payload["actual_received_date"] = delivery_day.isoformat()
    if explicit_month is not None:
        modern_payload["reconciliation_month"] = explicit_month
    modern_payload["idempotency_key"] = "p1-89-server-month-default"
    with TestClient(app) as client:
        _login(client, "finance")
        options = client.get("/api/finance/reconciliation-month-options")
        created = client.post("/api/finance/return_receipts", json=modern_payload)
        # v495 defaults to delivery date + customer cutoff, and retries retain
        # the original frozen month after the server crosses into another month.
        monkeypatch.setattr(finance_api, "beijing_today", lambda: date(2026, 9, 1))
        replay = client.post("/api/finance/return_receipts", json=modern_payload)
        periods = {month: client.get("/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": month})
            for month in ("2026-06", "2026-08", "2026-09")}

    assert options.json() == {
        "previous": "2026-07",
        "current": "2026-08",
        "next": "2026-09",
    }
    assert created.status_code == 201, created.text
    assert replay.json() == created.json()
    assert created.json()["reconciliation_month"] == expected_month
    assert created.json()["effective_reconciliation_month"] == expected_month
    for month, response in periods.items():
        assert response.status_code == 200, response.text
        assert [row["delivery_id"] for row in response.json()["deliveries"]] == ([1] if month == expected_month else [])
    with session_factory() as session:
        receipt = session.get(ReturnReceipt, created.json()["id"])
        receipt.reconciliation_month = None
        session.commit()
    with TestClient(app) as client:
        _login(client, "finance")
        # Historical NULL uses the original delivery/cycle fallback, even if
        # the modern request explicitly selected another month.
        legacy_month = "2026-09" if delivery_day == date(2026, 8, 20) else delivery_day.strftime("%Y-%m")
        legacy = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": legacy_month},
        )
    assert legacy.status_code == 200, legacy.text
    assert [row["delivery_id"] for row in legacy.json()["deliveries"]] == [1]


def test_p0_31_date_and_month_adjustments_are_versioned_and_lock_after_statement(
    finance_api_app,
) -> None:
    from app.models.delivery import Delivery
    from app.models.finance import ReturnReceipt

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        delivery = client.get("/api/deliveries/1")
        corrected = client.put(
            "/api/deliveries/1/actual-date",
            json={
                "actual_delivery_date": "2026-06-10",
                "expected_version": delivery.json()["version"],
                "idempotency_key": "p0-31-date-correction-001",
            },
        )
        replay = client.put(
            "/api/deliveries/1/actual-date",
            json={
                "actual_delivery_date": "2026-06-10",
                "expected_version": delivery.json()["version"],
                "idempotency_key": "p0-31-date-correction-001",
            },
        )
        stale = client.put(
            "/api/deliveries/1/actual-date",
            json={
                "actual_delivery_date": "2026-06-11",
                "expected_version": delivery.json()["version"],
                "idempotency_key": "p0-31-date-correction-stale",
            },
        )
        receipt_payload = {
            **_receipt_payload(),
            "reconciliation_month": "2026-06",
            "idempotency_key": "p0-31-receipt-create-001",
        }
        receipt = client.post(
            "/api/finance/return_receipts",
            json=receipt_payload,
        )
        adjusted = client.put(
            f"/api/finance/return_receipts/{receipt.json()['id']}/reconciliation-month",
            json={
                "reconciliation_month": "2026-07",
                "expected_version": receipt.json()["version"],
                "idempotency_key": "p0-31-month-adjust-001",
            },
        )
        adjusted_replay = client.put(
            f"/api/finance/return_receipts/{receipt.json()['id']}/reconciliation-month",
            json={
                "reconciliation_month": "2026-07",
                "expected_version": receipt.json()["version"],
                "idempotency_key": "p0-31-month-adjust-001",
            },
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-07",
                "delivery_ids": [1],
                "idempotency_key": "p0-31-statement-create-001",
            },
        )
        date_locked = client.put(
            "/api/deliveries/1/actual-date",
            json={
                "actual_delivery_date": "2026-06-12",
                "expected_version": corrected.json()["version"],
                "idempotency_key": "p0-31-date-after-statement",
            },
        )
        month_locked = client.put(
            f"/api/finance/return_receipts/{receipt.json()['id']}/reconciliation-month",
            json={
                "reconciliation_month": "2026-08",
                "expected_version": adjusted.json()["version"],
                "idempotency_key": "p0-31-month-after-statement",
            },
        )

    assert delivery.status_code == 200, delivery.text
    assert corrected.status_code == 200, corrected.text
    assert replay.json() == corrected.json()
    assert corrected.json()["version"] == delivery.json()["version"] + 1
    assert stale.status_code == 409
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["reconciliation_month"] == "2026-06"
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted_replay.json() == adjusted.json()
    assert adjusted.json()["reconciliation_month"] == "2026-07"
    assert adjusted.json()["actual_received_date"] == "2026-06-14"
    assert statement.status_code == 201, statement.text
    assert date_locked.status_code == 409
    assert date_locked.json()["detail"]["code"] == "delivery_actual_date_locked"
    assert month_locked.status_code == 409
    assert month_locked.json()["detail"]["code"] == "reconciliation_month_locked"
    with session_factory() as session:
        assert session.get(Delivery, 1).delivery_date == date(2026, 6, 10)
        stored_receipt = session.get(ReturnReceipt, receipt.json()["id"])
        assert stored_receipt.actual_received_date == date(2026, 6, 14)
        assert stored_receipt.reconciliation_month == "2026-07"


def test_p0_31_historical_backfill_requires_both_permissions_and_customer_scope(
    finance_api_app,
) -> None:
    from app.core.security import hash_password
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User

    app, session_factory = finance_api_app
    with session_factory() as session:
        admin_id = session.scalar(select(User.id).where(User.username == "admin"))
        denied_period = User(
            username="boss_without_period",
            password_hash=hash_password("RolePass123!"),
            role="boss",
            real_name="缺少周期权限",
            must_change_password=False,
        )
        out_of_scope = User(
            username="scoped_operator",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="无客户范围操作员",
            customer_access_mode="selected",
            must_change_password=False,
        )
        session.add_all([denied_period, out_of_scope])
        session.flush()
        session.add(
            UserPermissionOverride(
                user_id=denied_period.id,
                permission_code="finance.return_receipt.period.adjust",
                is_allowed=False,
                granted_by=admin_id,
            )
        )
        for code in (
            "deliveries.execute",
            "finance.return_receipt.period.adjust",
        ):
            session.add(
                UserPermissionOverride(
                    user_id=out_of_scope.id,
                    permission_code=code,
                    is_allowed=True,
                    granted_by=admin_id,
                )
            )
        session.commit()

    with TestClient(app) as client:
        _login(client, "boss_without_period")
        missing_permission = client.post(
            "/api/deliveries",
            json={
                **_historical_delivery_payload(),
                "idempotency_key": "p0-31-missing-period-001",
            },
        )
        _login(client, "scoped_operator")
        outside_scope = client.post(
            "/api/deliveries",
            json={
                **_historical_delivery_payload(),
                "idempotency_key": "p0-31-outside-scope-001",
            },
        )

    assert missing_permission.status_code == 403
    assert missing_permission.json()["detail"]["code"] == (
        "historical_delivery_permission_denied"
    )
    assert outside_scope.status_code == 403

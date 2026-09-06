from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from tests.test_p1_130_statement_invoice_finance import _login, p1_130_app


def _snapshot(factory) -> dict:
    from app.models.audit import OperationLog
    from app.models.finance import (
        Invoice,
        ReturnReceiptItem,
        SettlementRecord,
        Statement,
        StatementAdjustment,
        StatementItem,
    )
    from app.models.invoice_task import FinanceInvoiceTask

    with factory() as db:
        statement = db.get(Statement, 1)
        return {
            "statement": (
                statement.total_receivable,
                statement.total_gross_profit,
                statement.settled_amount,
                statement.invoiced_amount,
                statement.status,
                statement.confirmation_status,
                statement.confirmed_by,
                statement.confirmed_at,
                statement.version,
                statement.ledger_version,
            ),
            "lines": list(db.execute(select(StatementItem.id, StatementItem.return_receipt_item_id, StatementItem.receivable_amount).order_by(StatementItem.id)).all()),
            "receipts": list(db.execute(select(ReturnReceiptItem.id, ReturnReceiptItem.actual_received_quantity, ReturnReceiptItem.reconciliation_month_override, ReturnReceiptItem.reconciliation_override_reason).order_by(ReturnReceiptItem.id)).all()),
            "tasks": list(db.execute(select(FinanceInvoiceTask.id, FinanceInvoiceTask.status, FinanceInvoiceTask.version, FinanceInvoiceTask.voided_at).order_by(FinanceInvoiceTask.id)).all()),
            "payments": list(db.execute(select(SettlementRecord.id, SettlementRecord.settled_amount).order_by(SettlementRecord.id)).all()),
            "invoices": list(db.execute(select(Invoice.id, Invoice.invoice_amount).order_by(Invoice.id)).all()),
            "adjustments": list(db.scalars(select(StatementAdjustment.id).order_by(StatementAdjustment.id))),
            "audit": list(db.scalars(select(OperationLog.id).order_by(OperationLog.id))),
        }


def _dispute_payload(endpoint: str) -> dict:
    payload = {"expected_version": 2, "reason": "客户异议要求延后其中一条已收款明细"}
    if endpoint == "adjust-dispute":
        payload.update(
            remove_lines=[{"statement_item_id": 1, "target_month": "2026-09"}],
            add_return_receipt_item_ids=[],
        )
    return payload


@pytest.mark.parametrize("amount", ["40.00", "200.00"])
@pytest.mark.parametrize("endpoint", ["reopen", "adjust-dispute"])
def test_received_payment_blocks_uninvoiced_statement_dispute_without_writes(
    p1_130_app, amount: str, endpoint: str,
) -> None:
    app, factory = p1_130_app
    with TestClient(app) as client:
        _login(client)
        confirmed = client.post("/api/finance/statements/1/confirm", json={"expected_version": 1})
        assert confirmed.status_code == 200, confirmed.text
        received = client.put(
            "/api/finance/statements/1/settle",
            json={
                "expected_version": 2,
                "expected_ledger_version": 1,
                "amount": amount,
                "settlement_date": "2026-08-27",
                "account": "隔离测试收款",
                "idempotency_key": f"p045-paid-{endpoint}-{amount}",
            },
        )
        assert received.status_code == 200, received.text
        before = _snapshot(factory)
        assert before["statement"][2] == Decimal(amount)
        assert not before["invoices"]
        response = client.post(f"/api/finance/statements/1/{endpoint}", json=_dispute_payload(endpoint))
        after = _snapshot(factory)
        assert response.status_code == 409, (
            f"{endpoint}: received={amount}, before={before['statement']}, "
            f"after={after['statement']}; response={response.text}"
        )
        assert "收款" in response.text
        assert after == before


@pytest.mark.parametrize("evidence", ["summary_only", "record_only"])
@pytest.mark.parametrize("endpoint", ["reopen", "adjust-dispute"])
@pytest.mark.parametrize("confirmation_status", ["confirmed", "draft"])
def test_statement_dispute_checks_summary_and_payment_fact_independently(
    p1_130_app, evidence: str, endpoint: str, confirmation_status: str,
) -> None:
    from app.models.finance import SettlementRecord, Statement
    from app.models.invoice_task import FinanceInvoiceTask

    app, factory = p1_130_app
    with TestClient(app) as client:
        _login(client)
        assert client.post("/api/finance/statements/1/confirm", json={"expected_version": 1}).status_code == 200
        with factory() as db:
            if confirmation_status == "draft":
                db.get(Statement, 1).confirmation_status = "draft"
                for task in db.scalars(select(FinanceInvoiceTask)):
                    task.status = "voided"
            if evidence == "summary_only":
                db.get(Statement, 1).settled_amount = Decimal("40.00")
            else:
                db.add(SettlementRecord(statement_id=1, settled_amount=Decimal("40.00"), settlement_date=date(2026, 8, 27), account="隔离旧事实测试", created_by=1))
            db.commit()
        before = _snapshot(factory)
        response = client.post(f"/api/finance/statements/1/{endpoint}", json=_dispute_payload(endpoint))
        assert response.status_code == 409, response.text
        assert "收款" in response.text
        assert _snapshot(factory) == before


@pytest.mark.parametrize("endpoint", ["reopen", "adjust-dispute"])
def test_payment_committed_after_initial_statement_read_still_blocks_dispute(
    p1_130_app, endpoint: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import update

    from app.api import finance
    from app.models.finance import SettlementRecord, Statement

    app, factory = p1_130_app
    original = finance._statement_for_user
    after_payment = None

    def read_then_commit_payment(db, statement_id, user):
        nonlocal after_payment
        statement = original(db, statement_id, user)
        if after_payment is None:
            with factory() as writer:
                writer.execute(
                    update(Statement)
                    .where(Statement.id == statement_id)
                    .values(settled_amount=Decimal("40.00"), ledger_version=2)
                )
                writer.add(SettlementRecord(statement_id=statement_id, settled_amount=Decimal("40.00"), settlement_date=date(2026, 8, 27), account="隔离并发收款", created_by=1))
                writer.commit()
            after_payment = _snapshot(factory)
            assert statement.settled_amount == Decimal("0.00"), "the original session must retain its stale pre-payment read"
        return statement

    with TestClient(app) as client:
        _login(client)
        assert client.post("/api/finance/statements/1/confirm", json={"expected_version": 1}).status_code == 200
        monkeypatch.setattr(finance, "_statement_for_user", read_then_commit_payment)
        response = client.post(f"/api/finance/statements/1/{endpoint}", json=_dispute_payload(endpoint))
        assert response.status_code == 409, response.text
        assert "收款" in response.text
        assert after_payment is not None
        assert _snapshot(factory) == after_payment

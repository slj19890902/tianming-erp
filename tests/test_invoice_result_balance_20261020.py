from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.test_fin001_invoice_tasks import (
    fin001_app, _login, _prepare_exported_task,
)


@pytest.mark.parametrize(
    "receivable,already,amount,expected",
    [
        ("0.300", "0.100", "0.200", 200),
        ("0.299", "0.100", "0.200", 409),
    ],
)
def test_result_uses_three_decimal_balance_without_float_tail_or_overdraft(
    fin001_app, receivable, already, amount, expected,
):
    from app.models.finance import Statement, StatementItem, Invoice, FinanceManualMutation
    from app.models.invoice_task import FinanceInvoiceTask

    app, factory = fin001_app
    with factory() as db:
        statement = db.get(Statement, 1)
        statement.total_receivable = Decimal(receivable)
        item = db.get(StatementItem, 1)
        item.receivable_amount = Decimal(amount)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        task = _prepare_exported_task(client, idempotency_key="decimal-balance")
        # An already registered partial invoice occupies the remaining balance.
        partial = client.post("/api/finance/invoices", json={
            "statement_id": 1, "invoice_number": "EXISTING-PART",
            "invoice_date": "2026-10-20", "invoice_amount": already,
            "expected_version": 2, "expected_ledger_version": 1,
            "idempotency_key": "decimal-existing-part",
        })
        assert partial.status_code == 201, partial.text
        payload = dict(status="issued", invoice_number="REMAINING-PART",
                       invoice_date="2026-10-20", expected_version=2,
                       expected_ledger_version=2)
        issued = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=payload)
        assert issued.status_code == expected, issued.text
        if expected == 200:
            replay = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=payload)
            assert replay.status_code == 200 and replay.json() == issued.json()
            conflict = client.post(f"/api/finance/invoice-tasks/{task['id']}/result",
                                   json={**payload, "invoice_number": "OTHER-NUMBER"})
            assert conflict.status_code == 409
    with factory() as db:
        statement = db.get(Statement, 1)
        task_row = db.get(FinanceInvoiceTask, task["id"])
        assert statement.total_receivable == Decimal(receivable)
        assert statement.invoiced_amount == (
            Decimal(already) + Decimal(amount) if expected == 200 else Decimal(already)
        )
        assert statement.ledger_version == (3 if expected == 200 else 2)
        assert task_row.status == ("issued" if expected == 200 else "exported")
        assert db.query(Invoice).count() == (2 if expected == 200 else 1)
        assert db.query(FinanceManualMutation).count() == (2 if expected == 200 else 1)

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from tests.test_p1_130_statement_invoice_finance import _login, p1_130_app


def _seed_linked_payable(factory):
    from app.models.finance_payable import FinancePayable
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import SupplierMonthlyStatement

    with factory() as db:
        supplier = Supplier(
            standard_name="P045合成纸板供应商", normalized_name="p045合成纸板供应商",
            is_active=True,
        )
        db.add(supplier)
        db.flush()
        payable = FinancePayable(
            supplier_id=supplier.id, counterparty_name=supplier.standard_name,
            category="material", document_date=date(2026, 8, 20),
            amount=Decimal("1000.00"), status="confirmed",
            idempotency_key="p045-linked-payable", created_by=1,
        )
        db.add(payable)
        db.flush()
        statement = SupplierMonthlyStatement(
            statement_number="SUP-P045-LINKED", supplier_id=supplier.id,
            supplier_name_snapshot=supplier.standard_name, settlement_month="2026-08",
            period_start=date(2026, 7, 21), period_end=date(2026, 8, 20),
            currency="CNY", tax_basis="tax_inclusive", status="confirmed_pending_invoice",
            erp_amount=Decimal("1000.00"), adjusted_amount=Decimal("1000.00"),
            confirmed_amount=Decimal("1000.00"), finance_payable_id=payable.id,
            generated_by=1,
        )
        db.add(statement)
        db.commit()
        return payable.id, statement.id


def _linked_state(factory, payable_id, statement_id):
    from app.models.audit import OperationLog
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import SupplierMonthlyPayment, SupplierMonthlyStatement

    with factory() as db:
        payable = db.get(FinancePayable, payable_id)
        statement = db.get(SupplierMonthlyStatement, statement_id)
        return {
            "payable": (payable.status, payable.version, payable.amount,
                        payable.confirmed_at, payable.paid_at, payable.voided_at, payable.note),
            "statement": (statement.status, statement.version, statement.confirmed_amount,
                          statement.paid_amount, statement.invoice_allocated_amount),
            "payments": db.scalar(select(func.count()).select_from(SupplierMonthlyPayment)),
            "audit": db.scalar(select(func.count()).select_from(OperationLog)),
        }


@pytest.mark.parametrize("action", ["paid", "void", "confirm"])
def test_supplier_linked_payable_rejects_generic_transition_without_writes(p1_130_app, action):
    app, factory = p1_130_app
    payable_id, statement_id = _seed_linked_payable(factory)
    with TestClient(app) as client:
        _login(client)
        before = _linked_state(factory, payable_id, statement_id)
        response = client.post(
            f"/api/finance/payables/{payable_id}/{action}",
            json={"expected_version": 1, "reason": "合成回归：必须从供应商月结处理"},
        )
        after = _linked_state(factory, payable_id, statement_id)
        assert response.status_code == 409, f"{response.text}; persisted state: {after}"
        assert response.json()["detail"]["code"] == "FINANCE_PAYABLE_MANAGED_BY_SUPPLIER_SETTLEMENT"
        assert after == before


@pytest.mark.parametrize("action,expected_status", [("paid", "paid"), ("void", "voided")])
def test_manual_payable_can_still_confirm_and_transition(p1_130_app, action, expected_status):
    app, _factory = p1_130_app
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/finance/payables", json={
            "counterparty_name": "P045合成运费", "category": "freight",
            "document_date": "2026-09-07", "amount": "80.00",
            "idempotency_key": f"p045-manual-payable-{action}",
        })
        assert created.status_code == 201, created.text
        payable_id = created.json()["id"]
        confirmed = client.post(
            f"/api/finance/payables/{payable_id}/confirm", json={"expected_version": 1},
        )
        assert confirmed.status_code == 200, confirmed.text
        response = client.post(
            f"/api/finance/payables/{payable_id}/{action}",
            json={"expected_version": confirmed.json()["version"], "reason": "合成手工应付回归"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == expected_status

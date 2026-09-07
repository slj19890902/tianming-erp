from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from tests.test_p1_131_cost_pool import p1_131_cost_app
from tests.test_p1_149_supplier_settlement import (
    _include_supplier_router,
    _seed_payable_statement,
    _seed_supplier_credit,
    login_cost,
)


def _seed_prior_month_credit(factory, statement_id: int, currency: str) -> int:
    from app.models.supplier_settlement import SupplierMonthlyStatement

    with factory() as db:
        target = db.get(SupplierMonthlyStatement, statement_id)
        source = SupplierMonthlyStatement(
            statement_number=f"CREDIT-SOURCE-{currency}",
            supplier_id=target.supplier_id,
            supplier_name_snapshot=target.supplier_name_snapshot,
            settlement_month="2026-07",
            period_start=date(2026, 6, 21),
            period_end=date(2026, 7, 20),
            currency=currency,
            tax_basis=target.tax_basis,
            status="paid",
            erp_amount=Decimal("100.00"),
            adjustment_amount=Decimal("0.00"),
            adjusted_amount=Decimal("100.00"),
            supplier_statement_amount=Decimal("100.00"),
            confirmed_amount=Decimal("100.00"),
            invoice_allocated_amount=Decimal("100.00"),
            paid_amount=Decimal("100.00"),
            generation_origin="manual",
            generated_by=target.generated_by,
        )
        db.add(source)
        db.commit()
        source_id = source.id
    return _seed_supplier_credit(factory, statement_id=source_id, amount="100.00")


def _payment_state(factory, statement_id: int, credit_id: int) -> dict:
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import (
        SupplierCreditLot,
        SupplierMonthlyPayment,
        SupplierMonthlyStatement,
        SupplierPaymentBatch,
    )

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        credit = db.get(SupplierCreditLot, credit_id)
        payable = db.get(FinancePayable, statement.finance_payable_id)
        return {
            "statement": (statement.status, statement.paid_amount, statement.version),
            "credit": (credit.status, credit.available_amount, credit.version),
            "payable": (payable.status, payable.version),
            "batches": db.scalar(select(func.count(SupplierPaymentBatch.id))),
            "payments": db.scalar(select(func.count(SupplierMonthlyPayment.id))),
        }


@pytest.mark.parametrize("source_currency", ["CNY", "USD"])
def test_payment_batch_only_applies_credit_in_statement_currency(
    p1_131_cost_app, source_currency: str
) -> None:
    app, factory = p1_131_cost_app
    _include_supplier_router(app)
    _, statement_id = _seed_payable_statement(factory)
    credit_id = _seed_prior_month_credit(factory, statement_id, source_currency)
    before = _payment_state(factory, statement_id, credit_id)

    with TestClient(app) as client:
        login_cost(client)
        response = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payment-batches",
            json={
                "expected_version": 1,
                "payment_date": "2026-09-03",
                "credit_applications": [
                    {"credit_id": credit_id, "amount": "100.00", "expected_version": 1}
                ],
                "bank_amount": "900.00",
                "bank_reference": "CREDIT-CURRENCY-REVIEW",
                "idempotency_key": f"review-credit-currency-{source_currency.lower()}",
            },
        )

    after = _payment_state(factory, statement_id, credit_id)
    if source_currency == "CNY":
        assert response.status_code == 201
        assert after["statement"] == ("paid", Decimal("1000.00"), 2)
        assert after["credit"] == ("exhausted", Decimal("0.00"), 2)
        assert after["payable"] == ("paid", 2)
        assert after["batches"] == 1
        assert after["payments"] == 2
    else:
        assert {"status": response.status_code, "state": after} == {
            "status": 422,
            "state": before,
        }


def test_payment_options_and_detail_only_list_same_currency_credit(p1_131_cost_app) -> None:
    app, factory = p1_131_cost_app
    _include_supplier_router(app)
    _, statement_id = _seed_payable_statement(factory)
    same_currency_id = _seed_prior_month_credit(factory, statement_id, "CNY")
    _seed_prior_month_credit(factory, statement_id, "USD")

    with TestClient(app) as client:
        login_cost(client)
        response = client.get(
            f"/api/finance/supplier-settlements/{statement_id}/payment-options"
        )
        listing = client.get(
            "/api/finance/supplier-settlements",
            params={"settlement_month": "2026-08"},
        )

    assert response.status_code == 200
    assert listing.status_code == 200
    detail = next(row for row in listing.json()["items"] if row["id"] == statement_id)
    assert [credit["id"] for credit in response.json()["credits"]] == [same_currency_id]
    assert [credit["id"] for credit in detail["available_credits"]] == [same_currency_id]
    assert Decimal(detail["credit_available_amount"]) == Decimal("100.00")

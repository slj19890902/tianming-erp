"""Regression cases from the 2026-09-07 review, using isolated API fixtures."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from tests.test_phase11_requisition import _login, requisition_app
from tests.test_p1_132_supplier_monthly_settlement import (
    _include_supplier_router, _paperboard_receipt,
)
from tests.test_p1_131_cost_pool import p1_131_cost_app
from tests.test_p1_149_supplier_settlement import (
    _create_acceptance, _freeze_beijing_date, _include_all_p1_149_finance_routers,
    _seed_order, login_cost,
)
from tests.test_p1_144_simplified_finance import _seed_payable_statement
from tests.test_p1_81_receipt_purpose_flow import _use_p181_published_map_identity


def _reviewed_statement(client, factory):
    receipt_id = _paperboard_receipt(client, factory)
    response = client.post('/api/finance/supplier-settlements/generate', json={
        'settlement_month': '2026-08', 'idempotency_key': 'review-guards-generate',
    })
    assert response.status_code == 200, response.text
    statement = response.json()['items'][0]
    response = client.post(f"/api/finance/supplier-settlements/{statement['id']}/review", json={
        'expected_version': statement['version'],
        'supplier_statement_number': 'REVIEW-GUARDS',
        'supplier_statement_date': '2026-08-25',
        'supplier_statement_amount': statement['erp_amount'],
        'idempotency_key': 'review-guards-review',
    })
    assert response.status_code == 200, response.text
    return receipt_id, response.json()


@pytest.mark.parametrize('action', ['confirm', 'regenerate'])
def test_reversed_source_cannot_be_confirmed_or_regenerated_into_payable(
    requisition_app, monkeypatch, action,
):
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import SupplierMonthlyStatement

    app, factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client, 'admin')
        receipt_id, statement = _reviewed_statement(client, factory)
        response = client.put(f'/api/incoming/receipt-items/{receipt_id}/revert', json={
            'reason': '隔离测试：撤回错误实收', 'idempotency_key': 'review-guards-revert',
        })
        assert response.status_code == 200, response.text
        response = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/{action}", json={
                'expected_version': statement['version'],
                'idempotency_key': f'review-guards-{action}',
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == (
            'SUPPLIER_SETTLEMENT_REGENERATION_REQUIRED' if action == 'confirm'
            else 'SUPPLIER_SETTLEMENT_REGENERATION_EMPTY'
        )
    with factory() as db:
        saved = db.get(SupplierMonthlyStatement, statement['id'])
        assert saved.status == 'draft'
        assert saved.version == statement['version']
        assert saved.finance_payable_id is None
        assert db.scalar(select(func.count(FinancePayable.id))) == 0


@pytest.mark.parametrize('payment_date,valid', [
    ('2026-08-31', False), ('2026-09-01', True),
    ('2027-03-01', True), ('2027-03-02', False),
])
def test_payment_batch_respects_acceptance_date_boundaries(
    p1_131_cost_app, monkeypatch, payment_date, valid,
):
    from app.models.finance_simplified import FinanceAcceptanceNote
    from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierPaymentBatch

    app, factory = p1_131_cost_app
    _include_all_p1_149_finance_routers(app)
    _freeze_beijing_date(monkeypatch, date(2027, 3, 3))
    customer_id, statement_id = _seed_payable_statement(factory)
    _seed_order(factory, customer_id=customer_id, order_number='REVIEW-GUARDS-ORDER',
                order_date=date(2027, 2, 15))
    with TestClient(app) as client:
        login_cost(client)
        note = _create_acceptance(client, customer_id=customer_id,
                                  bill_number='REVIEW-GUARDS-BILL', amount='1000.00')
        response = client.post(f'/api/finance/supplier-settlements/{statement_id}/payment-batches', json={
            'expected_version': 1, 'payment_date': payment_date,
            'credit_applications': [], 'acceptance_note_id': note['id'],
            'expected_acceptance_version': note['version'], 'bank_amount': '0.00',
            'idempotency_key': 'review-guards-payment',
        })
        if valid:
            assert response.status_code == 201, response.text
        else:
            assert response.status_code == 422, response.text
            assert response.json()['detail']['code'] == 'ACCEPTANCE_ENDORSE_DATE_INVALID'
    with factory() as db:
        saved = db.get(SupplierMonthlyStatement, statement_id)
        acceptance = db.get(FinanceAcceptanceNote, note['id'])
        assert saved.paid_amount == Decimal('1000.00' if valid else '0.00')
        assert acceptance.status == ('endorsed' if valid else 'held')
        assert db.scalar(select(func.count(SupplierPaymentBatch.id))) == (1 if valid else 0)
        if not valid:
            assert saved.version == 1
            assert acceptance.version == note['version']

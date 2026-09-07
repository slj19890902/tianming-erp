"""Regression cases from the 2026-09-07 review, using isolated API fixtures."""

from datetime import date, datetime
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


@pytest.mark.parametrize('action', ['regenerate', 'refresh'])
def test_source_scan_issue_does_not_silently_replace_statement_with_partial_lines(
    requisition_app, monkeypatch, action,
):
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierMonthlyStatementLine
    from app.services import supplier_monthly_settlement as monthly
    from tests.test_p1_81_receipt_purpose_flow import (
        _seed_material_and_staging, _create_frozen_sources, _freeze_receipt_fact, _receive,
    )

    app, factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client, 'admin')
        _seed_material_and_staging(factory)
        sources = _create_frozen_sources(client, factory, order_quantity=10,
            purchase_total=10, order_purpose=10, stock_purpose=0, composite=True)
        assert len(sources) == 2
        receipt_ids = []
        for i, source in enumerate(sources):
            price = _freeze_receipt_fact(client, source, idempotency_key=f'review-partial-price-{i}')
            assert price.status_code == 200, price.text
            receipt = _receive(client, source, price.json(), quantity=10,
                idempotency_key=f'review-partial-receipt-{i}')
            assert receipt.status_code == 200, receipt.text
            receipt_ids.append(receipt.json()['receipt_item_id'])
        with factory() as db:
            for key in receipt_ids:
                item = db.get(IncomingReceiptItem, key)
                db.get(IncomingReceipt, item.receipt_id).received_at = datetime(2026, 8, 15, 3)
            db.commit()
        response = client.post('/api/finance/supplier-settlements/generate', json={
            'settlement_month': '2026-08', 'idempotency_key': 'review-partial-generate'})
        assert response.status_code == 200, response.text
        statement = response.json()['items'][0]
        with factory() as db:
            lines_before = [(line.id, line.source_key, line.active_guard) for line in db.scalars(
                select(SupplierMonthlyStatementLine).where(SupplierMonthlyStatementLine.statement_id == statement['id']))]
            assert len(lines_before) == 2
        original_scan = monthly._scan_candidates_for_bounds
        def scan_with_missing_source(db, **kwargs):
            candidates, issues = original_scan(db, **kwargs)
            # Inject one scanner diagnostic without damaging frozen business
            # facts. Both receipt/API transactions and replacement are real.
            key = f'paperboard:{receipt_ids[0]}'
            return [item for item in candidates if item.source_key != key], [*issues,
                monthly._issue(source_type='paperboard', source_key=key, receipt_number='TEST',
                    code='PAPERBOARD_FROZEN_PRICE_MISSING', message='测试来源缺少冻结结算价格')]
        monkeypatch.setattr(monthly, '_scan_candidates_for_bounds', scan_with_missing_source)
        if action == 'regenerate':
            result = client.post(f"/api/finance/supplier-settlements/{statement['id']}/regenerate", json={
                'expected_version': statement['version'], 'idempotency_key': 'review-partial-regenerate'})
            assert result.status_code == 409, result.text
            assert result.json()['detail']['code'] == 'SUPPLIER_SETTLEMENT_SOURCE_ISSUES'
        else:
            result = client.post('/api/finance/supplier-settlements/generate', json={
                'settlement_month': '2026-08', 'idempotency_key': 'review-partial-refresh'})
            assert result.status_code == 200, result.text
            assert result.json()['changed_statement_count'] == 0
            # Refresh already preserves scanner warnings and requires explicit
            # regeneration. Keep that existing behavior alongside this fix.
            assert any(issue['code'] == 'PAPERBOARD_FROZEN_PRICE_MISSING' for issue in result.json()['issues'])
        with factory() as db:
            saved = db.get(SupplierMonthlyStatement, statement['id'])
            assert (saved.version, saved.status, saved.erp_amount) == (
                statement['version'], statement['status'], Decimal(statement['erp_amount']))
            assert [(line.id, line.source_key, line.active_guard) for line in db.scalars(
                select(SupplierMonthlyStatementLine).where(SupplierMonthlyStatementLine.statement_id == saved.id))] == lines_before
            assert db.scalar(select(func.count(SupplierMonthlyStatement.id))) == 1


@pytest.mark.parametrize('action', ['confirm', 'regenerate', 'refresh'])
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
        if action == 'refresh':
            payload = {'settlement_month': '2026-08', 'idempotency_key': 'review-empty-month-refresh'}
            response = client.post('/api/finance/supplier-settlements/generate', json=payload)
            assert response.status_code == 200, response.text
            assert response.json()['changed_statement_count'] == 0
            assert any(issue['code'] == 'SUPPLIER_SETTLEMENT_REGENERATION_EMPTY'
                       and issue['source_key'] == str(statement['id']) for issue in response.json()['issues'])
            assert client.post('/api/finance/supplier-settlements/generate', json=payload).json() == response.json()
        else:
            response = client.post(
                f"/api/finance/supplier-settlements/{statement['id']}/{action}", json={
                'expected_version': statement['version'],
                'idempotency_key': f'review-guards-{action}',
                },
            )
            assert response.status_code == 409, response.text
            assert response.json()['detail']['code'] == (
                'SUPPLIER_SETTLEMENT_SOURCE_CHANGED' if action == 'confirm'
                else 'SUPPLIER_SETTLEMENT_REGENERATION_EMPTY'
            )
    with factory() as db:
        saved = db.get(SupplierMonthlyStatement, statement['id'])
        assert saved.status == 'draft'
        assert saved.version == statement['version']
        assert saved.finance_payable_id is None
        assert db.scalar(select(func.count(FinancePayable.id))) == 0
        assert db.scalar(select(func.count(SupplierMonthlyStatement.id))) == 1


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

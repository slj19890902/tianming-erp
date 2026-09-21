from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from fastapi.testclient import TestClient

from test_p1_130_statement_invoice_finance import p1_130_app, _login
from test_fin001_invoice_tasks import fin001_app, _login as invoice_login, _complete_invoice_profile


def test_same_month_price_correction_reopens_and_preserves_source(p1_130_app):
    from app.models.finance import StatementItem, ReturnReceiptItem
    from app.models.order import OrderItem

    app, factory = p1_130_app
    with TestClient(app) as client:
        _login(client)
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 1}).status_code == 200
        payload = {'expected_version': 2, 'reason': '双方核对更正单价',
                   'update_lines': [{'statement_item_id': 1, 'unit_price': '12.50'}]}
        response = client.post('/api/finance/statements/1/adjust-dispute', json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['statement_month'] == '2026-08'
        assert response.json()['confirmation_status'] == 'draft'
        assert Decimal(str(response.json()['total_receivable'])) == Decimal('225.00')
        assert client.post('/api/finance/statements/1/adjust-dispute', json=payload).status_code == 409
        with factory() as db:
            assert db.get(StatementItem, 1).unit_price_snapshot == Decimal('12.50')
            assert db.get(OrderItem, 1).unit_price == Decimal('10.00')
            assert db.get(ReturnReceiptItem, 1).actual_received_quantity == 10
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 3}).status_code == 200


def test_corrected_quantity_and_price_are_used_in_same_month_invoice(fin001_app):
    from app.models.finance import ReturnReceiptItem, StatementAdjustment

    app, factory = fin001_app
    with TestClient(app) as client:
        invoice_login(client, 'fin001-admin')
        _complete_invoice_profile(client)
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 1}).status_code == 200
        old = client.post('/api/finance/statements/1/invoice-tasks', json={'expected_version': 2, 'idempotency_key': 'before-dispute'}).json()
        response = client.post('/api/finance/statements/1/adjust-dispute', json={
            'expected_version': 2, 'reason': '双方认可结算数量和单价',
            'update_lines': [{'statement_item_id': 1, 'unit_price': '12.50'}]})
        assert response.status_code == 200, response.text
        assert Decimal(response.json()['total_receivable']) == Decimal('125.00')
        receipt_payload = {'expected_version': 1, 'actual_received_date': '2026-08-02',
                           'statement_id': 1, 'expected_statement_version': 3,
                           'statement_correction_reason': '客户核对实际签收为八个',
                           'idempotency_key': 'receipt-dispute-correction',
                           'items': [{'delivery_item_id': 1, 'actual_received_quantity': 8,
                                      'resolution_action': 'continue_delivery', 'difference_reason': '原回单数量录错'}]}
        corrected = client.put('/api/finance/return_receipts/1', json=receipt_payload)
        assert corrected.status_code == 200, corrected.text
        replay = client.put('/api/finance/return_receipts/1', json=receipt_payload)
        assert replay.status_code == 200 and replay.json() == corrected.json()
        changed = {**receipt_payload, 'signed_by': '不同内容'}
        assert client.put('/api/finance/return_receipts/1', json=changed).status_code == 409
        assert client.get(f"/api/finance/invoice-tasks/{old['id']}").json()['status'] == 'voided'
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 4}).status_code == 200
        task = client.post('/api/finance/statements/1/invoice-tasks', json={'expected_version': 5, 'idempotency_key': 'after-dispute'})
        assert task.status_code == 201, task.text
        assert Decimal(str(task.json()['total_amount'])) == Decimal('100.00')
        with factory() as db:
            assert db.get(ReturnReceiptItem, 1).actual_received_quantity == 8
            adjustment = db.scalar(select(StatementAdjustment).where(StatementAdjustment.action == 'correct_receipt_quantity'))
            assert json.loads(adjustment.details_json)['corrected'][0]['before_quantity'] == 10
            assert json.loads(adjustment.details_json)['corrected'][0]['after_quantity'] == 8


@pytest.mark.parametrize('updates,status', [
    ([{'statement_item_id': 1, 'unit_price': '-1'}], 422),
    ([{'statement_item_id': 1, 'unit_price': '12', 'quantity': 11}], 422),
    ([{'statement_item_id': 1, 'unit_price': '12', 'quantity': 0}], 422),
    ([{'statement_item_id': 1, 'unit_price': '12'}, {'statement_item_id': 999, 'unit_price': '10'}], 400),
])
def test_invalid_correction_rolls_back_confirmation_and_amounts(p1_130_app, updates, status):
    from app.models.finance import Statement, StatementItem, StatementAdjustment
    app, factory = p1_130_app
    with TestClient(app) as client:
        _login(client)
        client.post('/api/finance/statements/1/confirm', json={'expected_version': 1})
        response = client.post('/api/finance/statements/1/adjust-dispute', json={
            'expected_version': 2, 'reason': '验证更正边界', 'update_lines': updates})
        assert response.status_code == status, response.text
        with factory() as db:
            assert db.get(Statement, 1).confirmation_status == 'confirmed'
            assert db.get(Statement, 1).version == 2
            assert db.get(StatementItem, 1).unit_price_snapshot == Decimal('10.00')
            assert db.scalar(select(StatementAdjustment)) is None


def test_correction_permission_is_enforced(fin001_app):
    app, _ = fin001_app
    with TestClient(app) as client:
        invoice_login(client, 'fin001-sales')
        response = client.post('/api/finance/statements/1/adjust-dispute', json={
            'expected_version': 1, 'reason': '不得越权更正',
            'update_lines': [{'statement_item_id': 1, 'unit_price': '12'}]})
        assert response.status_code == 403


@pytest.mark.parametrize('case', ['stale', 'confirmed', 'paid', 'no_resolution', 'audit_failure'])
def test_receipt_correction_rejects_unsafe_changes_atomically(fin001_app, monkeypatch, case):
    from app.models.finance import ReturnReceiptItem, ReturnReceipt, Statement, StatementAdjustment
    from app.api import finance
    app, factory = fin001_app
    with factory() as db:
        if case == 'paid':
            db.get(Statement, 1).settled_amount = Decimal('1')
        if case == 'confirmed':
            db.get(Statement, 1).confirmation_status = 'confirmed'
        db.commit()
    if case == 'audit_failure':
        def fail(*args, **kwargs):
            raise RuntimeError('injected audit failure')
        monkeypatch.setattr(finance, '_audit', fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        invoice_login(client, 'fin001-admin')
        payload = {'expected_version': 1, 'actual_received_date': '2026-08-02',
                   'statement_id': 1, 'expected_statement_version': 99 if case == 'stale' else 1,
                   'statement_correction_reason': '更正实际签收数量', 'idempotency_key': 'guard-' + case,
                   'items': [{'delivery_item_id': 1, 'actual_received_quantity': 8,
                              'resolution_action': None if case == 'no_resolution' else 'continue_delivery'}]}
        response = client.put('/api/finance/return_receipts/1', json=payload)
        assert response.status_code == (500 if case == 'audit_failure' else 400 if case == 'no_resolution' else 409), response.text
        with factory() as db:
            assert db.get(ReturnReceiptItem, 1).actual_received_quantity == 10
            assert db.get(ReturnReceipt, 1).version == 1
            assert db.get(Statement, 1).total_receivable == Decimal('113.00')
            assert db.get(Statement, 1).version == 1
            assert db.scalar(select(StatementAdjustment)) is None

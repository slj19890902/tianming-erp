from decimal import Decimal
from io import BytesIO
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from test_p1_130_statement_invoice_finance import p1_130_app, _login


def split_statement(factory):
    from app.models.finance import Statement, StatementItem
    with factory() as db:
        first = db.get(Statement, 1)
        second = Statement(statement_number='ST-SPLIT', customer_id=first.customer_id,
                           statement_month=first.statement_month, total_receivable=Decimal('100'),
                           total_gross_profit=Decimal('0'))
        db.add(second)
        db.flush()
        db.get(StatementItem, 2).statement_id = second.id
        first.total_receivable = Decimal('100')
        # Deliberately select the later delivery first: export must re-sort.
        db.commit()
        return second.id


def test_manual_merge_preserves_sources_and_exports_all_lines(p1_130_app, tmp_path, monkeypatch):
    from app.models.finance import Statement, StatementItem, StatementAdjustment
    app, factory = p1_130_app
    from app.api.invoice_tasks import customer_router
    from test_fin001_invoice_tasks import _complete_invoice_profile
    app.include_router(customer_router, prefix='/api/customers')
    monkeypatch.setenv('ERP_INVOICE_EXPORT_DIR', str(tmp_path / 'exports'))
    other = split_statement(factory)
    from app.models.delivery import Delivery
    from datetime import date
    with factory() as db:
        db.get(Delivery, 1).delivery_date = date(2026, 8, 5)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(client)
        response = client.get(f'/api/finance/statements/{other}/merge-candidates')
        assert response.status_code == 200, response.text
        rows = response.json()['items']
        payload = {'sources': [{'statement_id': row['id'], 'expected_version': row['version'],
                              'expected_ledger_version': row['ledger_version']} for row in rows],
                   'idempotency_key': 'merge-test-001'}
        response = client.post(f'/api/finance/statements/{other}/merge', json=payload)
        assert response.status_code == 200, response.text
        merged = response.json()
        assert merged['id'] not in {1, other}
        assert Decimal(merged['total_receivable']) == 200
        assert merged['confirmation_status'] == 'draft'
        assert len(merged['items']) == 2
        replay = client.post(f'/api/finance/statements/{other}/merge', json=payload)
        assert replay.status_code == 200 and replay.json() == merged
        changed = {**payload, 'sources': [payload['sources'][0], {**payload['sources'][1], 'expected_version': 99}]}
        assert client.post(f'/api/finance/statements/{other}/merge', json=changed).status_code == 409
        exported = client.get(f"/api/finance/statements/{merged['id']}/customer-export.xlsx?sort_by=order_number")
        assert exported.status_code == 200
        sheet = load_workbook(BytesIO(exported.content)).active
        assert sheet['A5'].value < sheet['A6'].value
        assert sheet['H7'].value == 200
        for old in (1, other):
            detail = client.get(f'/api/finance/statements/{old}').json()
            assert detail['merged_into_statement_id'] == merged['id']
            assert len(detail['voided_versions'][0]['snapshot']['items']) == 1
            assert client.post(f'/api/finance/statements/{old}/confirm', json={'expected_version': 2}).status_code == 409
            assert client.post(f'/api/finance/statements/{old}/cancel').status_code == 409
            assert client.get(f'/api/finance/statements/{old}/customer-export.xlsx').status_code == 409
            assert client.post(f'/api/finance/statements/{old}/adjust-dispute', json={
                'expected_version': 2, 'reason': '不可恢复旧账单',
                'update_lines': [{'statement_item_id': 1, 'unit_price': '12'}]}).status_code == 409
        with factory() as db:
            assert len(db.scalars(select(StatementItem)).all()) == 2
            assert len(db.scalars(select(StatementAdjustment)).all()) == 2
            assert all(db.get(Statement, sid).confirmation_status == 'cancelled' for sid in (1, other))
        assert client.post(f"/api/finance/statements/{merged['id']}/confirm", json={'expected_version': 1}).status_code == 200
        task_response = client.post(f"/api/finance/statements/{merged['id']}/invoice-tasks",
            json={'expected_version': 2, 'idempotency_key': 'merged-single-invoice'})
        assert task_response.status_code == 201, task_response.text
        task = task_response.json()
        assert Decimal(str(task['total_amount'])) == 200
        from app.models.invoice_task import FinanceInvoiceTaskItem
        with factory() as db:
            assert len(db.scalars(select(FinanceInvoiceTaskItem).where(FinanceInvoiceTaskItem.task_id == task['id'])).all()) == 2
        assert client.post(f"/api/finance/invoice-tasks/{task['id']}/confirm", json={'expected_version': task['version']}).status_code == 200
        exported = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert exported.status_code == 200, exported.text
        sheet = load_workbook(BytesIO(exported.content)).active
        quantities = [row[4] for row in sheet.iter_rows(min_row=4, values_only=True) if len(row)>4 and isinstance(row[4], (int,float))]
        assert quantities == [5, 10]
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        result = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json={
            'status': 'issued', 'invoice_number': 'MERGED-ONE-INVOICE', 'invoice_date': '2026-09-26',
            'expected_version': task['version'], 'expected_ledger_version': task['ledger_version']})
        assert result.status_code == 200, result.text
        from app.models.finance import Invoice
        with factory() as db:
            assert len(db.scalars(select(Invoice)).all()) == 1
            assert db.get(Statement, merged['id']).invoiced_amount == Decimal('200')


@pytest.mark.parametrize('case', ['stale', 'ledger', 'month', 'customer', 'paid', 'invoiced', 'audit', 'scope', 'task', 'permission'])
def test_merge_rejects_unsafe_scope_atomically(p1_130_app, monkeypatch, case):
    from app.models.finance import Statement, StatementItem, StatementAdjustment
    from app.api import finance
    app, factory = p1_130_app
    other = split_statement(factory)
    if case == 'task':
        from app.api.invoice_tasks import customer_router
        app.include_router(customer_router, prefix='/api/customers')
    with factory() as db:
        row = db.get(Statement, other)
        if case == 'month': row.statement_month = '2026-09'
        if case == 'customer':
            from app.models.customer import Customer
            customer = Customer(name='另一客户', customer_code='OTHER', is_active=True)
            db.add(customer); db.flush(); row.customer_id = customer.id
        if case == 'paid': row.settled_amount = Decimal('1')
        if case == 'invoiced': row.invoiced_amount = Decimal('1')
        if case == 'scope': row.settlement_name_snapshot = '不同的冻结结算对象'
        if case == 'permission':
            from app.models.user import User
            user = db.scalar(select(User))
            user.role = 'finance'
            user.customer_access_mode = 'selected'
        db.commit()
    if case == 'audit':
        def fail(*args, **kwargs): raise RuntimeError('injected audit failure')
        monkeypatch.setattr(finance, '_audit', fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        if case == 'task':
            from test_fin001_invoice_tasks import _complete_invoice_profile
            _complete_invoice_profile(client)
            assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 1}).status_code == 200
            assert client.post('/api/finance/statements/1/invoice-tasks', json={
                'expected_version': 2, 'idempotency_key': 'occupied-task-merge'}).status_code == 201
            preview = client.get('/api/finance/statements/1/merge-candidates')
            assert '已有有效开票任务' in preview.json()['items'][0]['blocker']
        payload = {'sources': [
            {'statement_id': 1, 'expected_version': 2 if case == 'task' else 1, 'expected_ledger_version': 1},
            {'statement_id': other, 'expected_version': 99 if case == 'stale' else 1,
             'expected_ledger_version': 99 if case == 'ledger' else 1}], 'idempotency_key': 'merge-guard-' + case}
        response = client.post('/api/finance/statements/1/merge', json=payload)
        assert response.status_code == (500 if case == 'audit' else 403 if case == 'permission' else 409), response.text
        with factory() as db:
            assert db.get(StatementItem, 1).statement_id == 1
            assert db.get(StatementItem, 2).statement_id == other
            assert db.get(Statement, 1).version == (2 if case == 'task' else 1)
            assert len(db.scalars(select(Statement)).all()) == 2
            assert db.scalar(select(StatementAdjustment)) is None


@pytest.mark.parametrize('same_key', [True, False])
def test_concurrent_merge_has_one_active_statement(p1_130_app, same_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.models.finance import Statement, StatementItem
    app, factory = p1_130_app
    other = split_statement(factory)
    barrier = Barrier(2)
    def run(index):
        with TestClient(app) as client:
            _login(client)
            barrier.wait(timeout=10)
            return client.post('/api/finance/statements/1/merge', json={
                'sources': [{'statement_id': sid, 'expected_version': 1, 'expected_ledger_version': 1} for sid in (1, other)],
                'idempotency_key': 'concurrent-merge-' + str(0 if same_key else index)})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    assert sorted(r.status_code for r in results) == ([200, 200] if same_key else [200, 409])
    with factory() as db:
        active = db.scalars(select(Statement).where(Statement.confirmation_status != 'cancelled')).all()
        assert len(active) == 1 and active[0].total_receivable == Decimal('200')
        assert len(db.scalars(select(StatementItem)).all()) == 2

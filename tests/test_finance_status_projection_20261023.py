"""Synthetic facts: status/next action must survive queue selection and reversal."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from test_fin001_invoice_tasks import fin001_app, _login
from test_finance_workflow_20261023 import queue


@pytest.mark.parametrize('invoiced,paid,label,action,done', [
    (0,0,'待开票','invoice',False),
    (50,0,'部分开票','invoice',False),
    (113,0,'已完成开票','payment',False),
    (113,20,'已完成开票','payment',False),
    (113,113,'已完成','completed',True),
    (50,50,'部分开票','invoice',False),
])
def test_all_and_reconciled_project_actual_progress(fin001_app,invoiced,paid,label,action,done):
    from app.models.finance import Invoice, Statement, SettlementRecord
    app,factory=fin001_app
    with factory() as db:
        bill=db.get(Statement,1);bill.confirmation_status='confirmed'
        bill.invoiced_amount=invoiced;bill.settled_amount=paid
        bill.status='settled' if paid==113 else 'unsettled'
        if invoiced:
            db.add(Invoice(statement_id=1,invoice_number='SYNTHETIC-STATUS',
                invoice_date=date(2026,8,8),invoice_amount=invoiced))
        if paid:
            db.add(SettlementRecord(statement_id=1,settled_amount=paid,
                settlement_date=date(2026,8,9),account='synthetic'))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        for kind in ('all','reconciled'):
            group=queue(client,kind)['items'][0]
            assert (group['queue_status'],group['primary_action'],group['is_completed'])==(label,action,done)
            statement=group['statements'][0]
            assert (statement['queue_status'],statement['primary_action'],statement['is_completed'])==(label,action,done)
        if invoiced>paid:
            group=queue(client,'pending_payment','collections')['items'][0]
            assert group['queue_status']==('部分收款' if paid else '待收款')
    with factory() as db:
        bill=db.get(Statement,1)
        assert bill.confirmation_status=='confirmed' and bill.invoiced_amount==invoiced and bill.settled_amount==paid


def test_reconciled_subset_does_not_inherit_same_month_draft_progress(fin001_app):
    from app.models.finance import Statement
    app,factory=fin001_app
    with factory() as db:
        # Synthetic completed legacy facts; original draft remains a separate bill.
        db.add(Statement(statement_number='SYNTHETIC-DONE',customer_id=1,statement_month='2026-08',
            total_receivable=113,total_gross_profit=0,invoiced_amount=113,settled_amount=113,
            status='settled',confirmation_status='confirmed'))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        all_rows=queue(client,'all')
        assert all_rows['items'][0]['queue_status']=='待核对'
        history=queue(client,'reconciled')
        group=history['items'][0]
        assert [s['statement_number'] for s in group['statements']]==['SYNTHETIC-DONE']
        assert (group['queue_status'],group['primary_action'],group['is_completed'])==('已完成','completed',True)
        assert history['queue_counts']==all_rows['queue_counts']


def test_real_issue_payment_and_both_reversals_keep_status_in_sync(fin001_app):
    from test_invoice_reversals import issue,payload
    app,_=fin001_app
    with TestClient(app) as client:
        _login(client,'fin001-admin')
        invoice_id=issue(client)
        def assert_progress(label,action):
            for kind in ('all','reconciled'):
                group=queue(client,kind)['items'][0]
                assert (group['queue_status'],group['primary_action'])==(label,action)
                assert group['statements'][0]['queue_status']==label
        assert_progress('已完成开票','payment')
        for amount in (20,93):
            detail=client.get('/api/finance/statements/1').json()
            response=client.put('/api/finance/statements/1/settle',json={
                'amount':amount,'settlement_date':'2026-10-23','account':'synthetic',
                'expected_version':detail['version'],'expected_ledger_version':detail['ledger_version'],
                'idempotency_key':f'status-payment-{amount}'})
            assert response.status_code==200,response.text
            assert_progress('已完成' if amount==93 else '已完成开票','completed' if amount==93 else 'payment')
        detail=client.get('/api/finance/statements/1').json()
        for payment in detail['settlements']:
            current=client.get('/api/finance/statements/1').json()
            response=client.post(f"/api/finance/statements/1/settlements/{payment['id']}/reverse",json={
                'expected_version':current['version'],'expected_ledger_version':current['ledger_version'],
                'reason':'synthetic status reversal','idempotency_key':f"status-reverse-{payment['id']}"})
            assert response.status_code==200,response.text
            assert_progress('已完成开票','payment')
        response=client.post(f'/api/finance/invoices/{invoice_id}/void',json=payload(client,invoice_id))
        assert response.status_code==200,response.text
        assert queue(client,'reconciled')['items']==[]
        group=queue(client,'all')['items'][0]
        assert (group['queue_status'],group['primary_action'])==('待修改','reconcile')
        assert group['statements'][0]['queue_status']=='待修改'
        assert not group['is_completed']

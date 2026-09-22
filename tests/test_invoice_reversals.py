from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_fin001_invoice_tasks import fin001_app, _login, _prepare_exported_task
from test_partner_invoice_merge import prepare_partner, merge, issue_payload


def issue(client):
    task = _prepare_exported_task(client, idempotency_key='reversal-source')
    response = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json={
        'status':'issued', 'invoice_number':'REVERSE-ONE', 'invoice_date':'2026-08-08',
        'expected_version':task['version'], 'expected_ledger_version':task['ledger_version']})
    assert response.status_code == 200, response.text
    return client.get('/api/finance/invoice-tasks').json()['invoice_records'][0]['id']


def payload(client, invoice_id):
    result = client.get(f'/api/finance/invoices/{invoice_id}/void-preview')
    assert result.status_code == 200, result.text
    p = result.json()
    return {k:p[k] for k in ('expected_versions','expected_ledger_versions','expected_task_version')} | {
        'idempotency_key':'test-void-invoice', 'treatment':'tax_void', 'treatment_date':'2026-09-22',
        'reference':'隔离测试税局处理记录', 'reason':'隔离测试数量错误', 'confirmed':True}


def test_reversal_retains_invoice_and_snapshots_reopens_once(fin001_app):
    from app.models.finance import Invoice, Statement, FinanceIdempotencyRecord, StatementAdjustment
    from app.models.invoice_task import FinanceInvoiceTask, FinanceInvoiceTaskItem
    app, factory = fin001_app
    with TestClient(app) as c:
        _login(c)
        invoice_id=issue(c); p=payload(c,invoice_id)
        url=f'/api/finance/invoices/{invoice_id}/void'
        response=c.post(url,json=p)
        assert response.status_code==200,response.text
        assert response.json()['reopened_statement_ids']==[1]
        assert c.post(url,json=p).json()==response.json()
        assert c.post(url,json={**p,'reason':'different'}).status_code==409
        queue=c.get('/api/finance/current-customer-months',params={'all_open':True,'balance_type':'pending_reconciliation'}).json()
        assert queue['items'][0]['queue_status']=='待修改'
        detail=c.get('/api/finance/statements/1').json()
        assert detail['invoices'][0]['invoice_status']=='voided'
        assert c.get('/api/finance/invoice-tasks').json()['invoice_records'][0]['reversal']['reference']==p['reference']
    with factory() as db:
        invoice=db.get(Invoice,invoice_id); s=db.get(Statement,1)
        assert invoice.invoice_number=='REVERSE-ONE' and invoice.invoice_amount==113
        assert invoice.invoice_status=='voided' and s.invoiced_amount==0 and s.confirmation_status=='draft'
        assert s.ledger_version==3
        assert db.get(FinanceInvoiceTask,invoice.invoice_task_id).status=='voided'
        assert db.scalar(select(FinanceInvoiceTaskItem)) is not None
        assert len(db.scalars(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.action=='void_issued_invoice')).all())==1
        assert db.scalar(select(StatementAdjustment).where(StatementAdjustment.action=='void_issued_invoice_for_modify')) is not None


@pytest.mark.parametrize('blocker',['paid','stale','unconfirmed','unauthorized'])
def test_reversal_protects_payment_version_confirmation_and_permission(fin001_app, blocker):
    from app.models.finance import Statement, Invoice
    app,factory=fin001_app
    with TestClient(app) as c:
        _login(c); invoice_id=issue(c); p=payload(c,invoice_id)
        if blocker=='paid':
            with factory() as db:
                db.get(Statement,1).settled_amount=1;db.commit()
        elif blocker=='stale': p['expected_ledger_versions']['1']=1
        elif blocker=='unconfirmed':p['confirmed']=False
        else:_login(c,'fin001-sales')
        response=c.post(f'/api/finance/invoices/{invoice_id}/void',json=p)
        assert response.status_code in (403,409,422),response.text
    with factory() as db:
        assert db.get(Invoice,invoice_id).invoice_status=='issued'
        assert db.get(Statement,1).invoiced_amount==113


def test_merged_invoice_reverses_all_shares_atomically(fin001_app):
    from app.models.finance import Statement, Invoice
    app,factory=fin001_app
    with TestClient(app) as c:
        _login(c); prepare_partner(c,factory); task=merge(c).json()
        assert c.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx").status_code==200
        result=c.post(f"/api/finance/invoice-tasks/{task['id']}/result",json=issue_payload(task))
        assert result.status_code==200,result.text
        invoice_id=c.get('/api/finance/invoice-tasks').json()['invoice_records'][0]['id']
        p=payload(c,invoice_id)
        partial={**p,'expected_versions':{'1':p['expected_versions']['1']}}
        assert c.post(f'/api/finance/invoices/{invoice_id}/void',json=partial).status_code==409
        response=c.post(f'/api/finance/invoices/{invoice_id}/void',json=p)
        assert response.status_code==200,response.text
        assert set(response.json()['reopened_statement_ids'])=={1,2}
    with factory() as db:
        assert all(s.invoiced_amount==0 and s.confirmation_status=='draft' for s in db.scalars(select(Statement)))
        assert db.get(Invoice,invoice_id).invoice_amount==Decimal('339')


def test_old_statement_version_is_archived_without_duplicating_receivables(fin001_app):
    from app.models.finance import Statement, StatementItem
    from app.models.invoice_task import FinanceInvoiceTask
    app,factory=fin001_app
    with TestClient(app) as c:
        _login(c);task=_prepare_exported_task(c,idempotency_key='invalidate-source')
        p={'expected_version':2,'expected_ledger_version':1,'idempotency_key':'invalidate-old-version','reason':'价格待重新核对'}
        url='/api/finance/statements/1/invalidate-version'
        result=c.post(url,json=p)
        assert result.status_code==200,result.text
        assert c.post(url,json=p).json()==result.json()
        assert c.post(url,json={**p,'idempotency_key':'stale-second-key'}).status_code==409
        detail=c.get('/api/finance/statements/1').json()
        assert detail['confirmation_status']=='draft' and detail['version']==3
        assert len(detail['voided_versions'])==1
        assert len(detail['voided_versions'][0]['snapshot']['items'])==len(detail['items'])
        old=detail['voided_versions'][0]['snapshot']['items'][0]
        assert old['statement_item_id']==detail['items'][0]['statement_item_id']
        assert Decimal(str(old['receivable_amount']))==Decimal(str(detail['items'][0]['receivable_amount']))
        assert old['actual_received_quantity']==detail['items'][0]['actual_received_quantity']
        assert detail['voided_versions'][0]['snapshot']['confirmation_status']=='confirmed'
    with factory() as db:
        assert len(db.scalars(select(Statement)).all())==1
        assert db.get(Statement,1).total_receivable==113
        assert len(db.scalars(select(StatementItem)).all())==1
        assert db.get(FinanceInvoiceTask,task['id']).status=='voided'


def test_reversal_preserves_other_invoice_and_rolls_back_failed_audit(fin001_app, monkeypatch):
    from app.api import finance
    from app.models.finance import Invoice, Statement
    from datetime import date
    app,factory=fin001_app
    with TestClient(app) as c:
        _login(c); invoice_id=issue(c)
        # A separate remaining issued allocation must never be wiped away.
        with factory() as db:
            db.get(Statement,1).invoiced_amount=123
            db.get(Statement,1).total_receivable=200
            db.add(Invoice(statement_id=1,invoice_number='OTHER-ISSUED',invoice_date=date(2026,8,8),invoice_amount=10))
            db.commit()
        p=payload(c,invoice_id)
        original=finance._audit
        def fail(*args,**kwargs): raise RuntimeError('synthetic audit failure')
        monkeypatch.setattr(finance,'_audit',fail)
        with pytest.raises(RuntimeError,match='synthetic audit failure'):
            c.post(f'/api/finance/invoices/{invoice_id}/void',json=p)
        with factory() as db:
            assert db.get(Invoice,invoice_id).invoice_status=='issued'
            assert db.get(Statement,1).invoiced_amount==123
        monkeypatch.setattr(finance,'_audit',original)
        result=c.post(f'/api/finance/invoices/{invoice_id}/void',json=p)
        assert result.status_code==200,result.text
        assert result.json()['reopened_statement_ids']==[] and result.json()['retained_statement_ids']==[1]
        with factory() as db:
            assert db.get(Statement,1).invoiced_amount==10
            assert db.get(Statement,1).confirmation_status=='confirmed'


def test_concurrent_reversal_is_single_ledger_change(fin001_app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.models.finance import Statement
    app,factory=fin001_app
    with TestClient(app) as c:
        _login(c); invoice_id=issue(c); p=payload(c,invoice_id)
    barrier=Barrier(2)
    def run():
        with TestClient(app) as c:
            _login(c);barrier.wait(timeout=10)
            return c.post(f'/api/finance/invoices/{invoice_id}/void',json=p)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _: run(),range(2)))
    assert any(r.status_code==200 for r in responses)
    assert all(r.status_code in (200,409) for r in responses)
    with factory() as db:
        assert db.get(Statement,1).invoiced_amount==0 and db.get(Statement,1).ledger_version==3
    with TestClient(app) as c:
        _login(c); assert c.post(f'/api/finance/invoices/{invoice_id}/void',json=p).status_code==200

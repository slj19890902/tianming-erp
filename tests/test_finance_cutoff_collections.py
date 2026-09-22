from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_phase8_finance import finance_api_app, _login, _receipt_payload


def seed_cutoff(factory):
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.finance import Statement
    with factory() as db:
        db.get(Customer, 1).statement_cycle_start_day = 20
        db.get(Delivery, 1).delivery_date = date(2026, 9, 22)
        db.add(Statement(statement_number='CLOSED-SEP', customer_id=1, statement_month='2026-09',
            total_receivable=100, total_gross_profit=0, confirmation_status='confirmed', invoiced_amount=100))
        db.commit()


def test_new_receipt_uses_cutoff_and_default_queue_defers_next_month(finance_api_app, monkeypatch):
    from app.api import finance
    app, factory = finance_api_app
    monkeypatch.setattr(finance, 'beijing_today', lambda: date(2026, 9, 22))
    seed_cutoff(factory)
    with TestClient(app) as client:
        _login(client, 'finance')
        payload = {**_receipt_payload(), 'actual_received_date':'2026-09-22', 'idempotency_key':'cutoff-new-receipt'}
        response = client.post('/api/finance/return_receipts', json=payload)
        assert response.status_code == 201, response.text
        assert response.json()['reconciliation_month'] == '2026-10'
        assert client.post('/api/finance/return_receipts', json=payload).json()['id'] == response.json()['id']
        params = {'all_open':True, 'balance_type':'pending_reconciliation'}
        queued = client.get('/api/finance/current-customer-months', params=params).json()
        assert queued['total'] == 0 and queued['queue_counts']['pending_reconciliation'] == 0
        future = client.get('/api/finance/current-customer-months', params={**params,'statement_month':'2026-10'}).json()
        assert future['total'] == 1 and future['items'][0]['statement_month'] == '2026-10'
        monkeypatch.setattr(finance, 'beijing_today', lambda: date(2026, 10, 1))
        assert client.get('/api/finance/current-customer-months',params=params).json()['total'] == 1
        assert client.post('/api/finance/return_receipts',json=payload).status_code == 201


def test_manual_month_remains_explicit_and_history_is_not_rewritten(finance_api_app, monkeypatch):
    from app.api import finance
    from app.models.finance import ReturnReceipt
    app, factory = finance_api_app
    monkeypatch.setattr(finance, 'beijing_today', lambda: date(2026, 9, 22))
    seed_cutoff(factory)
    with TestClient(app) as client:
        _login(client, 'finance')
        response=client.post('/api/finance/return_receipts',json={**_receipt_payload(),
            'actual_received_date':'2026-09-22','reconciliation_month':'2026-09','idempotency_key':'manual-prior-period'})
        assert response.status_code == 201, response.text
        with factory() as db:
            assert db.scalar(select(ReturnReceipt)).reconciliation_month == '2026-09'


def test_reconciliation_and_collections_have_distinct_default_work(finance_api_app):
    from app.models.finance import Statement
    app, factory = finance_api_app
    with factory() as db:
        for number, confirmed, invoice in [('CHECK','draft',0),('BILL','confirmed',0),('COLLECT','confirmed',100)]:
            db.add(Statement(statement_number=number, customer_id=1, statement_month='2026-08',
                total_receivable=100,total_gross_profit=0,confirmation_status=confirmed,invoiced_amount=invoice))
        db.commit()
    with TestClient(app) as client:
        _login(client,'finance')
        params={'all_open':True,'workspace':'reconciliation'}
        result=client.get('/api/finance/current-customer-months',params=params)
        assert result.status_code==200,result.text
        bills=[s['statement_number'] for row in result.json()['items'] for s in row['statements']]
        assert set(bills)=={'CHECK','BILL'}
        collected=client.get('/api/finance/current-customer-months',params={'all_open':True,'workspace':'collections','balance_type':'pending_payment'}).json()
        assert [s['statement_number'] for row in collected['items'] for s in row['statements']]==['COLLECT']
        assert Decimal(str(collected['summary']['pending_payment_action_amount']))==100
        history=client.get('/api/finance/current-customer-months',params={**params,'customer_id':1,'balance_type':'all'}).json()
        assert len(history['items'][0]['statements'])==3

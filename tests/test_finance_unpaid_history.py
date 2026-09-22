from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_fin001_invoice_tasks import fin001_app, _login
from test_partner_invoice_merge import prepare_partner


def seed(client, factory):
    from app.models.customer import Customer
    from app.models.finance import Statement, Invoice, SettlementRecord
    prepare_partner(client, factory, create_tasks=False)
    with factory() as db:
        customer = Customer(customer_code='UNPAID', name='普通待收客户')
        db.add(customer); db.flush()
        ids = []
        for month, amount, paid in [('2026-08', 100, 20), ('2026-09', 200, 0), ('2026-10', 300, 0), ('2026-07', 50, 50)]:
            bill = Statement(statement_number=f'UNPAID-{month}', customer_id=customer.id,
                statement_month=month, total_receivable=amount, total_gross_profit=0,
                invoiced_amount=amount, settled_amount=paid, confirmation_status='confirmed',
                status='settled' if paid == amount else 'unsettled')
            db.add(bill); db.flush(); ids.append(bill.id)
            db.add(Invoice(statement_id=bill.id, invoice_number=f'REAL-{month}', invoice_amount=amount, invoice_date=date(2026, int(month[-2:]), 20)))
            if paid:
                db.add(SettlementRecord(statement_id=bill.id, settled_amount=paid, settlement_date=date(2026, int(month[-2:]), 21), account='银行到账'))
        db.commit()
        return customer.id, ids


def query(client, **kwargs):
    r = client.get('/api/finance/current-customer-months', params={'all_open': True, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()


def test_partner_member_loop_never_overwrites_customer_filter(fin001_app):
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client); customer, ids = seed(client, factory)
        result = query(client, balance_type='pending_payment')
        assert result['total'] == 3
        assert result['queue_counts']['pending_payment'] == 1
        assert {s['id'] for row in result['items'] for s in row['statements']} == set(ids[:3])
        selected = query(client, customer_id=customer, balance_type='pending_payment')
        assert selected['total'] == 3
        partner = query(client, customer_id=1, balance_type='reconciled')
        assert partner['total'] == 1 and len(partner['items'][0]['statements']) == 2


def test_unpaid_cutoff_paging_counts_and_partial_receipts(fin001_app):
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client); customer, ids = seed(client, factory)
        sep = query(client, statement_month='2026-09', balance_type='pending_payment', page_size=1)
        assert sep['total'] == 2 and sep['queue_counts']['pending_payment'] == 1
        assert Decimal(sep['summary']['pending_payment_action_amount']) == 280
        assert sep['items'][0]['statement_month'] == '2026-09'
        second = query(client, statement_month='2026-09', balance_type='pending_payment', page_size=1, page=2)
        assert second['items'][0]['statement_month'] == '2026-08'
        assert second['items'][0]['queue_status'] == '部分收款'
        aug = query(client, statement_month='2026-08', balance_type='pending_payment')
        assert aug['total'] == 1 and aug['items'][0]['statement_month'] == '2026-08'
        # The ordinary month API used by reports remains exact unless requested.
        exact = query(client, statement_month='2026-09', balance_type='reconciled', customer_id=customer)
        assert exact['total'] == 1
        through = query(client, statement_month='2026-09', through_month=True, balance_type='all', customer_id=customer)
        assert through['total'] == 3


def test_customer_search_includes_completed_and_trace(fin001_app):
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client); customer, ids = seed(client, factory)
        result = query(client, customer_id=customer)
        assert result['total'] == 4
        completed = next(row for row in result['items'] if row['statement_month'] == '2026-07')
        assert completed['is_completed']
        detail = client.get(f'/api/finance/statements/{ids[-1]}').json()
        assert detail['invoices'][0]['invoice_number'] == 'REAL-2026-07'
        assert detail['settlements'][0]['account'] == '银行到账'
        assert detail['confirmation_status'] == 'confirmed'
        all_rows = query(client, balance_type='all')
        assert all_rows['total'] == 5 and all_rows['queue_counts']['all'] == 2


def test_scope_still_excludes_unauthorized_customer(fin001_app):
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client); customer, ids = seed(client, factory)
        with factory() as db:
            user = db.scalar(select(User).where(User.username == 'fin001-finance'))
            user.customer_access_mode = 'selected'
            db.add(UserCustomerScope(user_id=user.id, customer_id=customer)); db.commit()
        result = query(client, balance_type='all', through_month=True, statement_month='2026-09')
        assert result['total'] == 3
        assert all(row['customer_id'] == customer for row in result['items'])
        assert client.get('/api/finance/current-customer-months', params={'customer_id':1}).status_code == 403


def test_partial_invoice_and_prepaid_balance_do_not_hide_other_bills(fin001_app):
    from app.models.finance import Statement
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client); customer, ids = seed(client, factory)
        with factory() as db:
            # Isolated projection fixture: one partly invoiced bill, one advance
            # fully covering issued amounts. Undownloaded/only downloaded tasks
            # cannot stand in for real invoice amounts.
            db.get(Statement, ids[0]).invoiced_amount = 60
            db.get(Statement, ids[1]).invoiced_amount = 0
            db.commit()
        result = query(client, statement_month='2026-09', balance_type='pending_payment')
        assert result['total'] == 1
        assert Decimal(result['summary']['pending_payment_action_amount']) == 40
        assert result['items'][0]['statements'][0]['id'] == ids[0]
        with factory() as db:
            db.get(Statement, ids[0]).settled_amount = 60; db.commit()
        assert query(client, statement_month='2026-09', balance_type='pending_payment')['total'] == 0
        assert query(client, customer_id=customer, balance_type='all')['total'] == 4

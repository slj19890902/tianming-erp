from decimal import Decimal
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select
from test_fin001_invoice_tasks import fin001_app, _login
from test_partner_invoice_merge import prepare_partner, issue_payload


def setup_customer(client, factory):
    from app.models.finance import Statement, StatementItem
    from app.models.delivery import Delivery
    prepare_partner(client, factory, create_tasks=False)
    with factory() as db:
        second = db.get(Statement, 2)
        second.customer_id = 1
        second.statement_month = '2026-09'
        db.get(Delivery, 2).customer_id = 1
        db.scalar(select(StatementItem).where(StatementItem.statement_id == 2)).source_customer_id = 1
        db.commit()


def preview(client, ids=(1, 2)):
    return client.post('/api/finance/statements/1/customer-invoice-preview', json={'statement_ids': list(ids)})


def merge(client, key='customer-merge-20261017'):
    data = preview(client)
    assert data.status_code == 200, data.text
    payload = {'statement_ids': [1, 2], 'scope_hash': data.json()['scope_hash'], 'idempotency_key': key}
    response = client.post('/api/finance/statements/1/customer-invoice-tasks', json=payload)
    return response, payload


def test_cross_month_merge_export_one_invoice_replay_and_visibility(fin001_app):
    from app.models.finance import Statement, StatementItem, Invoice
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        setup_customer(client, factory)
        # Existing separately saved/exported tasks must also be superseded.
        for sid in (1, 2):
            task = client.post(f'/api/finance/statements/{sid}/invoice-tasks', json={'expected_version': 2, 'idempotency_key': f'customer-source-{sid}'})
            assert task.status_code == 201, task.text
        response, payload = merge(client)
        assert response.status_code == 201, response.text
        task = response.json()
        assert task['customer_merge'] and len(task['source_statements']) == 2
        assert Decimal(task['total_amount']) == Decimal('339.000')
        replay = client.post('/api/finance/statements/1/customer-invoice-tasks', json=payload)
        assert replay.status_code == 201 and replay.json()['id'] == task['id']
        assert len(client.get('/api/finance/invoice-tasks').json()['items']) == 1
        changed = dict(payload, scope_hash='0' * 64)
        assert client.post('/api/finance/statements/1/customer-invoice-tasks', json=changed).status_code == 409
        exported = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert exported.status_code == 200, exported.text
        sheet = load_workbook(BytesIO(exported.content)).active
        assert sheet['G4'].number_format == '0.00'
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        issued = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=issue_payload(task))
        assert issued.status_code == 200, issued.text
        with factory() as db:
            assert db.query(Invoice).count() == 1
            assert db.get(Statement, 1).invoiced_amount == 113
            assert db.get(Statement, 2).invoiced_amount == 226
            assert len(db.scalars(select(StatementItem)).all()) == 2


def test_customer_merge_denials_stale_scope_and_atomic_failure(fin001_app, monkeypatch):
    from app.models.finance import Statement
    from app.models.invoice_task import FinanceInvoiceTask
    from app.api import invoice_tasks
    app, factory = fin001_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        prepare_partner(client, factory, create_tasks=False)
        assert preview(client).status_code == 409  # cross-customer, even same buyer
        with factory() as db:
            db.get(Statement, 2).customer_id = 1
            db.commit()
        data = preview(client).json()
        with factory() as db:
            db.get(Statement, 2).ledger_version += 1
            db.commit()
        payload = {'statement_ids': [1, 2], 'scope_hash': data['scope_hash'], 'idempotency_key': 'stale-customer-merge'}
        assert client.post('/api/finance/statements/1/customer-invoice-tasks', json=payload).status_code == 409
        with factory() as db:
            db.get(Statement, 2).invoiced_amount = Decimal('1')
            db.commit()
        assert preview(client).json()['blockers']
        with factory() as db:
            db.get(Statement, 2).invoiced_amount = 0
            db.commit()
        real_audit = invoice_tasks._audit
        def fail_audit(*args, **kwargs):
            if kwargs.get('action') == 'MERGE_CUSTOMER_INVOICE_TASKS':
                raise RuntimeError('test late rollback')
            return real_audit(*args, **kwargs)
        monkeypatch.setattr(invoice_tasks, '_audit', fail_audit)
        response, _ = merge(client, 'failure-customer-merge')
        assert response.status_code == 500
        with factory() as db:
            assert db.query(FinanceInvoiceTask).count() == 0


def test_money_three_places_and_exact_legacy_frozen_split(fin001_app):
    from app.api.invoice_tasks import _frozen_exclusive_parts
    from app.models.finance import Statement, StatementItem
    from app.models.invoice_task import FinanceInvoiceTask, FinanceInvoiceTaskItem
    app, factory = fin001_app
    with factory() as db:
        row = db.get(Statement, 1)
        row.total_receivable = Decimal('113.001')
        db.commit()
        db.expire_all()
        assert db.get(Statement, 1).total_receivable == Decimal('113.001')
    assert _frozen_exclusive_parts(Decimal('3'), Decimal('1.111'), Decimal('.13'), Decimal('3.766')) == (Decimal('3.333'), Decimal('.433'))
    assert _frozen_exclusive_parts(Decimal('3'), Decimal('1.111'), Decimal('.13'), Decimal('3.76')) == (Decimal('3.33'), Decimal('.43'))
    assert _frozen_exclusive_parts(Decimal('3'), Decimal('1.111'), Decimal('.13'), Decimal('4')) is None


def test_customer_profile_mode_does_not_rewrite_historical_prices(fin001_app):
    from test_fin001_invoice_tasks import _complete_invoice_profile
    from app.models.finance import StatementItem
    from app.models.order import OrderItem
    from app.models.delivery import DeliveryItem
    from app.models.product import Product
    app, factory = fin001_app
    def history():
        with factory() as db:
            return [(model.__tablename__, [tuple(getattr(row, column.name) for column in model.__table__.columns)
                for row in db.scalars(select(model).order_by(model.__table__.c.id)).all()])
                for model in (StatementItem, OrderItem, DeliveryItem, Product)]
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(client)
        before = history()
        current = client.get('/api/customers/1/invoice-profile').json()
        payload = {key: current[key] for key in ('invoice_title','tax_no','invoice_address','invoice_phone','bank_name','bank_account','default_seller_id','default_tax_rate','is_enabled','confirmation_status')}
        payload.update(price_tax_mode='tax_exclusive', expected_version=current['version'])
        response = client.put('/api/customers/1/invoice-profile', json=payload)
        assert response.status_code == 200, response.text
        assert before == history()


def test_new_price_three_places_amount_two_places_invoice_file(fin001_app):
    from test_fin001_invoice_tasks import _complete_invoice_profile
    from app.models.finance import Statement, StatementItem
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(client, price_tax_mode='tax_exclusive', fill_unit_price=True)
        with factory() as db:
            statement = db.get(Statement, 1)
            statement.total_receivable = Decimal('3.77')
            statement.confirmation_status = 'confirmed'
            statement.version = 2
            row = db.scalar(select(StatementItem))
            row.actual_received_quantity = 3
            row.unit_price_snapshot = Decimal('1.111')
            row.price_tax_mode_snapshot = 'tax_exclusive'
            row.receivable_amount = Decimal('3.76629')
            db.commit()
        created = client.post('/api/finance/statements/1/invoice-tasks', json={'expected_version':2,'idempotency_key':'third-decimal-invoice'})
        assert created.status_code == 201, created.text
        task = created.json()
        assert Decimal(task['net_amount']) == Decimal('3.33')
        assert Decimal(task['tax_amount']) == Decimal('.43')
        assert Decimal(task['total_amount']) == Decimal('3.77')
        confirmed = client.post(f"/api/finance/invoice-tasks/{task['id']}/confirm", json={'expected_version':task['version']})
        assert confirmed.status_code == 200, confirmed.text
        response = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert response.status_code == 200, response.text
        sheet = load_workbook(BytesIO(response.content)).active
        assert Decimal(str(sheet['F4'].value)) == Decimal('1.111')
        assert Decimal(str(sheet['G4'].value)) == Decimal('3.333')
        assert sheet['F4'].number_format == '0.000'
        assert sheet['G4'].number_format == '0.00'


def test_pending_receivable_uses_frozen_delivery_terms(fin001_app):
    from app.api.finance import _pending_statement_query
    from app.models.delivery import DeliveryItem
    from app.models.order import OrderItem
    from app.models.finance import ReturnReceiptItem
    from app.services.delivery_snapshots import sales_contract
    app, factory = fin001_app
    with factory() as db:
        line = db.get(DeliveryItem, 1)
        line.sales_contract_json = sales_contract(unit="只", price=Decimal("1.115"), tax_mode="tax_exclusive", tax_rate=Decimal(".13"), source={"kind":"test"})
        from app.models.finance import StatementItem
        db.delete(db.scalar(select(StatementItem)))
        db.get(ReturnReceiptItem, 1).actual_received_quantity = 3
        db.get(OrderItem, 1).unit_price = Decimal("555")
        db.flush()
        rows = [dict(v) for v in db.execute(_pending_statement_query(1)).mappings()]
        assert rows and Decimal(str(rows[0]["unit_price"])) == Decimal("1.115")
        assert Decimal(str(rows[0]["receivable_amount"])) == Decimal("3.77985")

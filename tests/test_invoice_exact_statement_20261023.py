from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_fin001_invoice_tasks import fin001_app, _login
from test_customer_invoice_merge_20261017 import setup_customer, preview, merge
from test_partner_invoice_merge import issue_payload


def set_exact_sources(factory, amounts=('6909.2804', '1021.748')):
    from app.models.finance import Statement, StatementItem
    with factory() as db:
        for sid, amount in enumerate(amounts, 1):
            statement = db.get(Statement, sid)
            statement.total_receivable = Decimal(amount)
            line = db.scalar(select(StatementItem).where(StatementItem.statement_id == sid))
            line.receivable_amount = Decimal(amount)
            line.actual_received_quantity = 1
            line.unit_price_snapshot = Decimal(amount)
        db.commit()


@pytest.mark.parametrize('existing', [False, True])
def test_exact_statement_merge_export_issue_keeps_sales_facts(fin001_app, existing):
    from app.models.finance import Statement, Invoice
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        setup_customer(client, factory)
        set_exact_sources(factory)
        if existing:
            for sid in (1, 2):
                created = client.post(f'/api/finance/statements/{sid}/invoice-tasks', json={
                    'expected_version': 2, 'idempotency_key': f'exact-source-{sid}'})
                assert created.status_code == 201, created.text
        data = preview(client).json()
        assert not data['blockers'], data
        assert Decimal(str(data['total_amount'])) == Decimal('7931.03')
        response, payload = merge(client, f'exact-merge-{existing}')
        assert response.status_code == 201, response.text
        task = response.json()
        exported = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert exported.status_code == 200, exported.text
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        result_payload = issue_payload(task)
        result = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=result_payload)
        assert result.status_code == 200, result.text
        replay = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=result_payload)
        assert replay.status_code == 200 and replay.json() == result.json()
        pending = client.get('/api/finance/statements', params={'customer_id':1, 'balance_type':'pending_invoice'}).json()
        assert pending['total'] == 0, pending
        dashboard = client.get('/api/finance/current-customer-months', params={
            'customer_id':1, 'balance_type':'pending_invoice', 'all_open':True}).json()
        assert Decimal(str(dashboard['summary']['pending_invoice_amount'])) == 0, dashboard
        with factory() as db:
            assert db.get(Statement, 1).total_receivable == Decimal('6909.2804')
            assert db.get(Statement, 2).total_receivable == Decimal('1021.748')
            assert db.get(Statement, 1).invoiced_amount == Decimal('6909.28')
            assert db.get(Statement, 2).invoiced_amount == Decimal('1021.75')
            assert db.query(Invoice).count() == 1


@pytest.mark.parametrize('change', ['amount', 'quantity', 'version'])
def test_frozen_same_cent_source_change_is_not_rounding(fin001_app, change):
    from app.models.finance import Statement, StatementItem
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        setup_customer(client, factory)
        set_exact_sources(factory)
        for sid in (1, 2):
            assert client.post(f'/api/finance/statements/{sid}/invoice-tasks', json={
                'expected_version': 2, 'idempotency_key': f'exact-stale-{sid}'}).status_code == 201
        with factory() as db:
            if change == 'amount':
                db.get(Statement, 1).total_receivable = Decimal('6909.2805')
                db.scalar(select(StatementItem).where(StatementItem.statement_id == 1)).receivable_amount = Decimal('6909.2805')
            elif change == 'quantity':
                db.scalar(select(StatementItem).where(StatementItem.statement_id == 1)).actual_received_quantity = 2
            else:
                db.get(Statement, 1).version += 1
            db.commit()
        assert preview(client).json()['blockers']


def test_manual_subcent_balance_is_not_silently_closed(fin001_app):
    from app.models.finance import Statement
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        setup_customer(client, factory)
        set_exact_sources(factory)
        with factory() as db:
            db.get(Statement, 1).invoiced_amount = Decimal('6909.28')
            db.commit()
        pending = client.get('/api/finance/statements', params={'customer_id':1, 'balance_type':'pending_invoice'}).json()
        assert {row['id'] for row in pending['items']} == {1, 2}
        assert preview(client).json()['blockers']


def test_twelve_line_statement_amounts_export_as_9107_03(fin001_app):
    from app.models.finance import Statement, StatementItem, ReturnReceiptItem
    from app.models.delivery import DeliveryItem
    app, factory = fin001_app
    groups = [
        [(103,'4.41'),(200,'4.41'),(100,'5.8506'),(200,'3.26'),(200,'2.45'),
         (100,'4.41'),(144,'2.3716'),(519,'3.92'),(1000,'1.029')],
        [(300,'3.92'),(200,'1.96'),(210,'2.9988')],
    ]
    with TestClient(app) as client:
        _login(client)
        setup_customer(client, factory)
        with factory() as db:
            for sid, rows in enumerate(groups, 1):
                original = db.scalar(select(StatementItem).where(StatementItem.statement_id == sid))
                rr = db.get(ReturnReceiptItem, original.return_receipt_item_id).return_receipt_id
                db.delete(original)
                db.flush()
                total = Decimal(0)
                for sequence, (qty, price) in enumerate(rows):
                    amount = qty * Decimal(price)
                    total += amount
                    delivery = DeliveryItem(delivery_id=sid, source_type='unordered_finished', product_id=1,
                        delivered_quantity=qty, product_code_snapshot=f'EXACT-{sid}-{sequence}',
                        product_name_snapshot='合成精确金额纸箱', unit_snapshot='只',
                        unit_price_snapshot=Decimal(price), price_source='manual')
                    db.add(delivery); db.flush()
                    receipt = ReturnReceiptItem(return_receipt_id=rr, delivery_item_id=delivery.id,
                        actual_received_quantity=qty)
                    db.add(receipt); db.flush()
                    db.add(StatementItem(statement_id=sid, source_customer_id=1,
                        return_receipt_item_id=receipt.id, actual_received_quantity=qty,
                        unit_price_snapshot=Decimal(price), unit_cost_snapshot=0,
                        receivable_amount=amount, gross_profit_amount=0,
                        price_tax_mode_snapshot='tax_inclusive', tax_rate_snapshot=Decimal('.13')))
                db.get(Statement, sid).total_receivable = total
            db.commit()
        result, _ = merge(client, 'twelve-line-exact-amounts')
        assert result.status_code == 201, result.text
        task = result.json()
        assert Decimal(str(task['total_amount'])) == Decimal('9107.03')
        exported = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert exported.status_code == 200, exported.text
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        assert len(task['items']) == 12
        assert sum(Decimal(str(row['quantity'])) for row in task['items']) == 3276
        result = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=issue_payload(task))
        assert result.status_code == 200, result.text

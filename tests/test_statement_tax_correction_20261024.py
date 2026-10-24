from decimal import Decimal
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from test_fin001_invoice_tasks import fin001_app, _login, _complete_invoice_profile


def correction(version=2, rate='0.13', **extra):
    return {'expected_version': version, 'reason': '根据实际发票核准本批税率',
            'tax_evidence_reference': '实际发票UAT-TAX-CORRECTION-13',
            'update_lines': [{'statement_item_id': 1, 'unit_price': '11.30', 'tax_rate': rate}],
            **extra}


def prepare_zero_tax(client, factory):
    from app.models.finance import StatementItem
    from app.models.order import OrderItem
    _complete_invoice_profile(client)
    with factory() as db:
        db.get(StatementItem, 1).tax_rate_snapshot = Decimal('0')
        db.get(OrderItem, 1).tax_rate_snapshot = Decimal('0')
        db.commit()
    assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 1}).status_code == 200
    response = client.post('/api/finance/statements/1/invoice-tasks', json={
        'expected_version': 2, 'idempotency_key': 'before-tax-correction'})
    assert response.status_code == 201, response.text
    assert Decimal(response.json()['tax_amount']) == 0
    return response.json()


@pytest.mark.parametrize('autoflush', [True, False])
def test_correct_tax_preserves_gross_and_source_then_rebuilds_task(fin001_app, autoflush):
    from app.models.finance import StatementItem, StatementAdjustment, Statement
    from app.models.order import OrderItem
    app, factory = fin001_app
    factory.configure(autoflush=autoflush)
    with TestClient(app) as client:
        _login(client, 'fin001-admin')
        old = prepare_zero_tax(client, factory)
        response = client.post('/api/finance/statements/1/adjust-dispute', json=correction())
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail['confirmation_status'] == 'draft'
        assert Decimal(str(detail['total_receivable'])) == Decimal('113')
        assert Decimal(str(detail['items'][0]['tax_rate_snapshot'])) == Decimal('.13')
        assert client.get(f"/api/finance/invoice-tasks/{old['id']}").json()['status'] == 'voided'
        with factory() as db:
            assert db.get(OrderItem, 1).tax_rate_snapshot == 0
            assert db.get(OrderItem, 1).unit_price == Decimal('11.30')
            assert db.get(StatementItem, 1).actual_received_quantity == 10
            adjustment = db.scalar(select(StatementAdjustment))
            audit = json.loads(adjustment.details_json)
            assert audit['tax_evidence_reference'] == '实际发票UAT-TAX-CORRECTION-13'
            assert audit['corrected'][0]['before']['tax_rate'] == '0.0000'
            assert Decimal(audit['corrected'][0]['after']['tax_rate']) == Decimal('.13')
            assert db.get(Statement, 1).version == 3
        assert client.post('/api/finance/statements/1/adjust-dispute', json=correction()).status_code == 409
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 3}).status_code == 200
        new = client.post('/api/finance/statements/1/invoice-tasks', json={
            'expected_version': 4, 'idempotency_key': 'after-tax-correction'})
        assert new.status_code == 201, new.text
        assert Decimal(new.json()['total_amount']) == Decimal('113')
        assert Decimal(new.json()['net_amount']) == Decimal('100')
        assert Decimal(new.json()['tax_amount']) == Decimal('13')


@pytest.mark.parametrize('reference', [None, '', '   '])
def test_tax_correction_requires_explicit_evidence(fin001_app, reference):
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client, 'fin001-admin')
        prepare_zero_tax(client, factory)
        response = client.post('/api/finance/statements/1/adjust-dispute',
                               json=correction(tax_evidence_reference=reference))
        assert response.status_code == 422, response.text
        assert '税率更正须填写实际票据或确认依据' in response.text


@pytest.mark.parametrize('rate', ['-0.01', '1.01', 'NaN', '0.1300001'])
def test_invalid_tax_rate_does_not_reopen_or_void_task(fin001_app, rate):
    from app.models.finance import Statement, StatementItem
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client, 'fin001-admin')
        old = prepare_zero_tax(client, factory)
        result = client.post('/api/finance/statements/1/adjust-dispute', json=correction(rate=rate))
        assert result.status_code == 422, result.text
        with factory() as db:
            assert db.get(Statement, 1).version == 2
            assert db.get(Statement, 1).confirmation_status == 'confirmed'
            assert db.get(StatementItem, 1).tax_rate_snapshot == 0
        assert client.get(f"/api/finance/invoice-tasks/{old['id']}").json()['status'] != 'voided'


@pytest.mark.parametrize('case', ['paid', 'issued', 'invoice_balance_without_record', 'audit_failure', 'other_source', 'permission'])
def test_tax_correction_uses_existing_finance_guards_and_rolls_back(fin001_app, monkeypatch, case):
    from app.models.finance import Statement, StatementItem, StatementAdjustment
    from app.api import finance
    from test_partner_invoice_merge import issue_payload
    app, factory = fin001_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, 'fin001-admin')
        old = prepare_zero_tax(client, factory)
        payload = correction()
        if case == 'paid':
            with factory() as db:
                db.get(Statement, 1).settled_amount = Decimal('1'); db.commit()
        elif case == 'invoice_balance_without_record':
            with factory() as db:
                db.get(Statement, 1).invoiced_amount = Decimal('1'); db.commit()
        elif case == 'issued':
            assert client.post(f"/api/finance/invoice-tasks/{old['id']}/confirm", json={'expected_version': 1}).status_code == 200
            assert client.get(f"/api/finance/invoice-tasks/{old['id']}/tax-template.xlsx").status_code == 200
            task = client.get(f"/api/finance/invoice-tasks/{old['id']}").json()
            assert client.post(f"/api/finance/invoice-tasks/{old['id']}/result", json=issue_payload(task)).status_code == 200
        elif case == 'audit_failure':
            def fail(*args, **kwargs):
                raise RuntimeError('injected tax correction audit failure')
            monkeypatch.setattr(finance, '_audit', fail)
        elif case == 'other_source':
            payload['update_lines'][0]['statement_item_id'] = 9999
        else:
            _login(client, 'fin001-sales')
        result = client.post('/api/finance/statements/1/adjust-dispute', json=payload)
        expected = {'paid': 409, 'issued': 409, 'invoice_balance_without_record': 409, 'audit_failure': 500, 'other_source': 400, 'permission': 403}[case]
        assert result.status_code == expected, result.text
        with factory() as db:
            assert db.get(Statement, 1).version == 2
            assert db.get(Statement, 1).confirmation_status == 'confirmed'
            assert db.get(StatementItem, 1).tax_rate_snapshot == 0
            assert db.scalar(select(StatementAdjustment)) is None


def test_explicit_exclusive_tax_correction_recalculates_receivable(fin001_app):
    from app.models.finance import StatementItem, StatementAdjustment
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client, 'fin001-admin')
        prepare_zero_tax(client, factory)
        with factory() as db:
            db.get(StatementItem, 1).price_tax_mode_snapshot = 'tax_exclusive'; db.commit()
        result = client.post('/api/finance/statements/1/adjust-dispute', json=correction())
        assert result.status_code == 200, result.text
        assert Decimal(str(result.json()['total_receivable'])) == Decimal('127.69')
        with factory() as db:
            log = json.loads(db.scalar(select(StatementAdjustment)).details_json)
            assert Decimal(log['corrected'][0]['before']['amount']) == Decimal('113')
            assert Decimal(log['corrected'][0]['after']['amount']) == Decimal('127.69')


def test_three_bills_zero_tax_and_stale_buyer_correct_then_merge_existing_invoice(fin001_app):
    from app.models.finance import Statement, StatementItem, ReturnReceiptItem, Invoice
    from app.models.delivery import DeliveryItem
    from app.models.invoice_task import CustomerInvoiceProfile
    from test_customer_invoice_merge_20261017 import setup_customer
    from test_partner_invoice_merge import issue_payload
    app, factory = fin001_app
    groups = {
        1: [(103,'4.41'),(200,'4.41'),(100,'5.8506'),(200,'3.26'),(200,'2.45'),
            (100,'4.41'),(144,'2.3716'),(519,'3.92'),(1000,'1.029')],
        2: [(200,'1.96'),(210,'2.9988')],
        3: [(300,'3.92')],
    }
    with TestClient(app) as client:
        _login(client, 'fin001-admin')
        setup_customer(client, factory)
        with factory() as db:
            anchor = db.get(Statement, 1)
            anchor.settlement_entity_id = None
            anchor.settlement_name_snapshot = None
            db.get(Statement, 2).settlement_entity_id = None
            db.get(Statement, 2).settlement_name_snapshot = None
            db.add(Statement(id=3, statement_number='ST-UAT-THIRD', customer_id=1,
                statement_month='2026-09', settlement_entity_id=anchor.settlement_entity_id,
                settlement_name_snapshot=anchor.settlement_name_snapshot,
                total_receivable=0, total_gross_profit=0, confirmation_status='confirmed', version=2))
            originals = db.scalars(select(StatementItem)).all()
            receipt_ids = {row.statement_id: db.get(ReturnReceiptItem, row.return_receipt_item_id).return_receipt_id for row in originals}
            for row in originals:
                db.delete(row)
            db.flush()
            for sid, rows in groups.items():
                total = Decimal(0)
                delivery_id = 1 if sid == 1 else 2
                for sequence, (qty, price) in enumerate(rows):
                    amount = qty * Decimal(price); total += amount
                    delivery = DeliveryItem(delivery_id=delivery_id, source_type='unordered_finished', product_id=1,
                        delivered_quantity=qty, product_code_snapshot=f'UAT-{sid}-{sequence}',
                        product_name_snapshot='合成三单开票纸箱', unit_snapshot='只',
                        unit_price_snapshot=Decimal(price), price_source='manual')
                    db.add(delivery); db.flush()
                    receipt = ReturnReceiptItem(return_receipt_id=receipt_ids[delivery_id],
                        delivery_item_id=delivery.id, actual_received_quantity=qty)
                    db.add(receipt); db.flush()
                    db.add(StatementItem(statement_id=sid, source_customer_id=1,
                        return_receipt_item_id=receipt.id, actual_received_quantity=qty,
                        unit_price_snapshot=Decimal(price), unit_cost_snapshot=0,
                        receivable_amount=amount, gross_profit_amount=0,
                        price_tax_mode_snapshot='tax_inclusive',
                        tax_rate_snapshot=Decimal(0) if sid == 2 else Decimal('.13')))
                db.get(Statement, sid).total_receivable = total
            profile = db.scalar(select(CustomerInvoiceProfile).where(CustomerInvoiceProfile.customer_id == 1))
            profile.settlement_entity_id = None
            correct_buyer = profile.tax_no
            profile.tax_no = 'OLD-PLACEHOLDER'
            db.commit()
        old = {}
        for sid in (1, 3, 2):
            if sid == 2:
                with factory() as db:
                    db.scalar(select(CustomerInvoiceProfile).where(CustomerInvoiceProfile.customer_id == 1)).tax_no = correct_buyer
                    db.commit()
            result = client.post(f'/api/finance/statements/{sid}/invoice-tasks', json={
                'expected_version': 2, 'idempotency_key': f'old-three-bill-{sid}'})
            assert result.status_code == 201, result.text
            old[sid] = result.json()['id']
        initial = client.post('/api/finance/statements/1/customer-invoice-preview', json={'statement_ids': [1,2,3]}).json()
        assert not initial['blockers']
        assert Decimal(str(initial['tax_amount'])) == Decimal('930.17')
        denied = client.post('/api/finance/statements/1/customer-invoice-tasks', json={
            'statement_ids': [1,2,3], 'scope_hash': initial['scope_hash'], 'idempotency_key': 'blocked-old-three-bill'})
        assert denied.status_code == 409, denied.text
        assert '冻结购方' in denied.text
        with factory() as db:
            rows = db.scalars(select(StatementItem).where(StatementItem.statement_id == 2)).all()
            updates = [{'statement_item_id': row.id, 'unit_price': str(row.unit_price_snapshot), 'tax_rate': '.13'} for row in rows]
        result = client.post('/api/finance/statements/2/adjust-dispute', json={
            'expected_version': 2, 'reason': '原税率录错，实际发票13%',
            'tax_evidence_reference': '实际发票UAT-9107.03', 'update_lines': updates})
        assert result.status_code == 200, result.text
        assert Decimal(str(result.json()['total_receivable'])) == Decimal('1021.748')
        for sid in (1, 3):
            result = client.post(f'/api/finance/statements/{sid}/reopen', json={
                'expected_version': 2, 'reason': '购方档案已纠正，撤回旧冻结开票资料'})
            assert result.status_code == 200, result.text
        for sid in (1,2,3):
            assert client.get(f"/api/finance/invoice-tasks/{old[sid]}").json()['status'] == 'voided'
            assert client.post(f'/api/finance/statements/{sid}/confirm', json={'expected_version': 3}).status_code == 200
        preview = client.post('/api/finance/statements/1/customer-invoice-preview', json={'statement_ids': [1,2,3]}).json()
        assert not preview['blockers'], preview
        merged = client.post('/api/finance/statements/1/customer-invoice-tasks', json={
            'statement_ids': [1,2,3], 'scope_hash': preview['scope_hash'], 'idempotency_key': 'corrected-three-bill-merge'})
        assert merged.status_code == 201, merged.text
        task = merged.json()
        assert Decimal(str(task['total_amount'])) == Decimal('9107.03')
        assert Decimal(str(task['net_amount'])) == Decimal('8059.31')
        assert Decimal(str(task['tax_amount'])) == Decimal('1047.72')
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        assert len(task['items']) == 12
        assert sum(Decimal(str(row['quantity'])) for row in task['items']) == 3276
        assert client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx").status_code == 200
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        payload = issue_payload(task)
        payload['invoice_number'] = 'UAT-EXISTING-9107.03'
        registered = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=payload)
        assert registered.status_code == 200, registered.text
        replay = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=payload)
        assert replay.status_code == 200
        with factory() as db:
            from app.models.invoice_task import FinanceInvoiceTask
            assert json.loads(db.get(FinanceInvoiceTask, task['id']).buyer_snapshot_json)['tax_no'] == correct_buyer
            assert db.query(Invoice).count() == 1
            assert sum(db.get(Statement, sid).total_receivable for sid in (1,2,3)) == Decimal('9107.0284')
            assert db.get(Statement, 2).total_receivable == Decimal('1021.748')

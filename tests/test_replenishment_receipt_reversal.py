from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_stock_replenishment_flow import (
    stock_replenishment_app, _login, _customer_replenishment_payload,
    _confirm_replenishment_purchase,
)
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
from app.models.stock_preparation import StockPreparationJob
from app.models.warehouse_inventory import InventoryLot


def create_stock(client, quantity=30):
    result = client.post('/api/requisition/stock-replenishment/orders',
                         json=_customer_replenishment_payload(quantity))
    assert result.status_code == 201, result.text
    return result.json()


def receive(client, stock, quantity=None):
    amount = quantity or stock['items'][0]['quantity']
    payload = dict(received_quantity=amount, idempotency_key=str(uuid4()))
    if amount + stock.get('_test_received', 0) < stock['items'][0]['quantity']:
        payload['resolution_action'] = 'await_supplier'
    result = client.put(f"/api/incoming/receive/sr{stock['items'][0]['id']}", json=payload)
    assert result.status_code == 200, result.text
    stock['_test_received'] = stock.get('_test_received', 0) + amount
    return result.json()


def cancel_jobs(client, receipt_id):
    row = next(r for r in client.get('/api/production/stock-preparation').json()['items']
               if r['receipt_item_id'] == receipt_id)
    for job in row['jobs']:
        if job['status'] != 'pending':
            continue
        result = client.post(f'/api/production/stock-preparation/{receipt_id}/actions', json=dict(
            action='cancel', operation_key=str(uuid4()), lot_version=row['lot_version'],
            job_id=job['id'], job_version=job['version']))
        assert result.status_code == 200, result.text
        row = next(r for r in client.get('/api/production/stock-preparation').json()['items']
                   if r['receipt_item_id'] == receipt_id)


def revert(client, receipt_id, **payload):
    return client.put(f'/api/incoming/receipt-items/{receipt_id}/revert', json=payload)


@pytest.fixture
def reversal_app(stock_replenishment_app):
    from app.api.stock_preparation import router
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    return app, factory


def test_cancel_then_reverse_preserves_history_and_replay(reversal_app):
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client, 500)
        _confirm_replenishment_purchase(client, stock)
        fact = receive(client, stock)
        blocked = revert(client, fact['receipt_item_id'])
        assert blocked.status_code == 409
        assert '生产安排' in blocked.text
        cancel_jobs(client, fact['receipt_item_id'])
        key = str(uuid4())
        result = revert(client, fact['receipt_item_id'], idempotency_key=key)
        assert result.status_code == 200, result.text
        assert revert(client, fact['receipt_item_id'], idempotency_key=key).json() == result.json()
        assert revert(client, fact['receipt_item_id'], idempotency_key=key, reason='another').status_code == 409
    with factory() as db:
        receipt = db.get(IncomingReceiptItem, fact['receipt_item_id'])
        lot = db.get(InventoryLot, receipt.received_inventory_lot_id)
        item = db.get(StockReplenishmentOrderItem, stock['items'][0]['id'])
        assert receipt.status == receipt.receipt.status == 'reversed'
        assert receipt.received_quantity == 500 and lot.status == 'closed'
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (0, 0, 0)
        assert item.stocked_quantity == 0 and item.inventory_lot_id is None
        assert db.get(StockReplenishmentOrder, stock['id']).status == 'confirmed'
        assert db.scalar(select(StockPreparationJob).where(StockPreparationJob.receipt_item_id == receipt.id)).status == 'cancelled'


def test_latest_receipt_first_and_exact_remaining_projection(reversal_app):
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client, 30)
        _confirm_replenishment_purchase(client, stock)
        first = receive(client, stock, 10)
        second = receive(client, stock, 20)
        cancel_jobs(client, first['receipt_item_id'])
        cancel_jobs(client, second['receipt_item_id'])
        assert revert(client, first['receipt_item_id']).status_code == 409
        result = revert(client, second['receipt_item_id'])
        assert result.status_code == 200, result.text
        with factory() as db:
            item = db.get(StockReplenishmentOrderItem, stock['items'][0]['id'])
            assert item.stocked_quantity == 10
            assert item.inventory_lot_id == db.get(IncomingReceiptItem, first['receipt_item_id']).received_inventory_lot_id
            assert db.get(StockReplenishmentOrder, stock['id']).status == 'partially_stocked'
        assert revert(client, first['receipt_item_id']).status_code == 200


@pytest.mark.parametrize('damage', ['quantity_consumed', 'quantity_reserved', 'quantity_damaged', 'quantity_scrapped', 'source_ref_id'])
def test_receipt_reversal_rejects_usage_or_wrong_source(reversal_app, damage):
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        fact = receive(client, stock)
        cancel_jobs(client, fact['receipt_item_id'])
        with factory() as db:
            lot = db.get(InventoryLot, fact['received_inventory_lot_id'])
            setattr(lot, damage, 1 if damage != 'source_ref_id' else 99999)
            db.commit()
        assert revert(client, fact['receipt_item_id']).status_code == 409
        with factory() as db:
            assert db.get(IncomingReceiptItem, fact['receipt_item_id']).status == 'posted'


def test_receipt_reversal_audit_failure_rolls_back(reversal_app, monkeypatch):
    app, factory = reversal_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        fact = receive(client, stock)
        cancel_jobs(client, fact['receipt_item_id'])
        from app.services import replenishment_receipt_reversal as service
        monkeypatch.setattr(service, 'append_audit_event', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('audit fault')))
        assert revert(client, fact['receipt_item_id']).status_code == 500
    with factory() as db:
        assert db.get(IncomingReceiptItem, fact['receipt_item_id']).status == 'posted'
        assert db.get(InventoryLot, fact['received_inventory_lot_id']).quantity_available == 30


def test_reversed_receipt_can_receive_new_batch_without_reopening_history(reversal_app):
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        first = receive(client, stock)
        cancel_jobs(client, first['receipt_item_id'])
        assert revert(client, first['receipt_item_id']).status_code == 200
        stock['_test_received'] = 0
        next_receipt = receive(client, stock)
        assert next_receipt['receipt_item_id'] != first['receipt_item_id']
        assert next_receipt['received_inventory_lot_id'] != first['received_inventory_lot_id']
    with factory() as db:
        assert db.get(IncomingReceiptItem, first['receipt_item_id']).status == 'reversed'
        assert db.get(InventoryLot, first['received_inventory_lot_id']).status == 'closed'
        assert db.get(StockReplenishmentOrderItem, stock['items'][0]['id']).stocked_quantity == 30
        assert db.get(InventoryLot, next_receipt['received_inventory_lot_id']).quantity_reserved == 30


@pytest.mark.parametrize('same_key', [True, False])
def test_concurrent_reversal_replays_same_key_and_rejects_other_key(reversal_app, monkeypatch, same_key):
    from app.api import incoming as api
    from app.models.purchase_receipt import IncomingReceiptReversalFact
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        fact = receive(client, stock)
        cancel_jobs(client, fact['receipt_item_id'])
        barrier, state = Barrier(2), local()
        original = api._reversal_idempotency_contract

        def overlap(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs['target_id'] == fact['receipt_item_id'] and not getattr(state, 'entered', False):
                state.entered = True
                barrier.wait(timeout=10)
            return result

        monkeypatch.setattr(api, '_reversal_idempotency_contract', overlap)
        key = str(uuid4())
        keys = [key, key if same_key else str(uuid4())]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda current: revert(client, fact['receipt_item_id'], idempotency_key=current), keys))
        assert sorted(r.status_code for r in results) == ([200, 200] if same_key else [200, 409])
        if same_key:
            assert results[0].json() == results[1].json()
    with factory() as db:
        records = list(db.scalars(select(IncomingReceiptReversalFact).where(
            IncomingReceiptReversalFact.incoming_receipt_item_id == fact['receipt_item_id'])))
        assert len(records) == 1
        assert db.get(InventoryLot, fact['received_inventory_lot_id']).quantity_available == 0


def test_complete_then_existing_production_reverse_then_receipt_reverse(reversal_app):
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        fact = receive(client, stock)
        row = next(r for r in client.get('/api/production/stock-preparation').json()['items']
                   if r['receipt_item_id'] == fact['receipt_item_id'])
        job = next(j for j in row['jobs'] if j['status'] == 'pending')
        processed = client.post(f"/api/production/stock-preparation/{fact['receipt_item_id']}/actions", json=dict(
            action='complete', operation_key=str(uuid4()), lot_version=row['lot_version'],
            job_id=job['id'], job_version=job['version'], actual_input_quantity=30, actual_output=30,
            location_id=7, layout_version=1, output_kind='semi'))
        assert processed.status_code == 200, processed.text
        assert revert(client, fact['receipt_item_id']).status_code == 409
        with factory() as db:
            completed = db.get(StockPreparationJob, processed.json()['completed_job_id'])
            output_id = completed.output_lot_id
            versions = dict(job_id=completed.id, job_version=completed.version,
                            source_version=db.get(InventoryLot, fact['received_inventory_lot_id']).version,
                            output_version=db.get(InventoryLot, output_id).version)
        restored = client.post(f"/api/production/stock-preparation/completions/job:{versions['job_id']}/revert",
            json=dict(operation_key=str(uuid4()), confirm_unused=True, jobs=[versions]))
        assert restored.status_code == 200, restored.text
        result = revert(client, fact['receipt_item_id'])
        assert result.status_code == 200, result.text
    with factory() as db:
        assert db.get(InventoryLot, output_id).status == 'closed'
        assert db.get(InventoryLot, fact['received_inventory_lot_id']).status == 'closed'


def test_receipt_reversal_respects_customer_scope(reversal_app, monkeypatch):
    from app.api import incoming as api
    from fastapi import HTTPException
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        fact = receive(client, stock)
        cancel_jobs(client, fact['receipt_item_id'])
        monkeypatch.setattr(api, 'require_customer_access', lambda *a, **kw: (_ for _ in ()).throw(HTTPException(403, 'customer scope')))
        assert revert(client, fact['receipt_item_id']).status_code == 403
    with factory() as db:
        assert db.get(IncomingReceiptItem, fact['receipt_item_id']).status == 'posted'
        assert db.get(InventoryLot, fact['received_inventory_lot_id']).quantity_available == 30


def test_confirmed_supplier_statement_prevents_reversal_of_stock_receipt(reversal_app):
    from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierMonthlyStatementLine
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        fact = receive(client, stock)
        cancel_jobs(client, fact['receipt_item_id'])
        with factory() as db:
            statement = SupplierMonthlyStatement(statement_number='reversal-frozen-statement', supplier_id=1,
                supplier_name_snapshot='苏州佳丰', settlement_month='2026-10',
                period_start=date(2026, 9, 21), period_end=date(2026, 10, 20), currency='CNY',
                tax_basis='tax_inclusive', status='confirmed_pending_invoice', confirmed_amount=30)
            db.add(statement)
            db.flush()
            db.add(SupplierMonthlyStatementLine(statement_id=statement.id, source_type='paperboard',
                source_key=f"paperboard:{fact['receipt_item_id']}", incoming_receipt_item_id=fact['receipt_item_id'],
                purchase_document_number='stock-purchase', receipt_number=fact['receipt_number'],
                receipt_date=date(2026, 10, 10), category_label='纸板', material_or_product_snapshot='A416D',
                received_quantity=30, quantity_unit='张', frozen_unit_price=1, price_unit='sheet',
                currency='CNY', tax_basis='tax_inclusive', tax_rate=0, erp_amount=30, tax_amount=0,
                source_link=f"receipt:{fact['receipt_item_id']}"))
            db.commit()
        result = revert(client, fact['receipt_item_id'])
        assert result.status_code == 409
        assert 'SUPPLIER_SETTLEMENT_RECEIPT_FROZEN' in result.text
    with factory() as db:
        assert db.get(IncomingReceiptItem, fact['receipt_item_id']).status == 'posted'
        assert db.get(InventoryLot, fact['received_inventory_lot_id']).quantity_available == 30

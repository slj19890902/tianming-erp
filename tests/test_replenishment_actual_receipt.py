import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_stock_replenishment_flow import (
    stock_replenishment_app, _login, _customer_replenishment_payload,
)


@pytest.mark.parametrize('actual,action', [(101, None), (99, 'accept_short'), (99, 'await_supplier')])
def test_actual_receipt_preserves_plan_and_inventory(stock_replenishment_app, actual, action):
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import InventoryLot
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        result = client.post('/api/requisition/stock-replenishment/orders', json=_customer_replenishment_payload(quantity=100))
        assert result.status_code == 201, result.text
        item_id = result.json()['items'][0]['id']
        payload = dict(received_quantity=actual, resolution_action=action, idempotency_key='actual-receipt')
        response = client.put(f'/api/incoming/receive/sr{item_id}', json=payload)
        assert response.status_code == 200, response.text
        replay = client.put(f'/api/incoming/receive/sr{item_id}', json=payload)
        assert replay.status_code == 200, replay.text
        changed = client.put(f'/api/incoming/receive/sr{item_id}', json={**payload, 'received_quantity': actual+1})
        assert changed.status_code == 409, changed.text
        pending = client.get('/api/incoming/pending').json()['items']
        assert any(r['item_id'] == f'sr{item_id}' for r in pending) == (action == 'await_supplier')
    with factory() as db:
        item = db.get(StockReplenishmentOrderItem, item_id)
        assert item.quantity == 100
        assert item.stocked_quantity == min(actual, 100)
        receipts = db.scalars(select(IncomingReceiptItem)).all()
        assert len(receipts) == 1
        assert receipts[0].received_quantity == actual
        assert receipts[0].variance_quantity == actual-100
        lots = db.scalars(select(InventoryLot)).all()
        assert len(lots) == 1
        assert lots[0].quantity_available == actual


@pytest.mark.parametrize('finish', ['over', 'short', 'later_close'])
def test_partial_receipts_and_short_closure(stock_replenishment_app, finish):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.warehouse_inventory import InventoryLot
    from app.services.stock_preparation import list_rows
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        source = _customer_replenishment_payload(quantity=100)
        source['items'].append(dict(source['items'][0], quantity=20))
        created = client.post('/api/requisition/stock-replenishment/orders', json=source)
        assert created.status_code == 201, created.text
        item_id = created.json()['items'][0]['id']
        first = client.put(f'/api/incoming/receive/sr{item_id}', json=dict(
            received_quantity=60, resolution_action='await_supplier', idempotency_key='first'))
        assert first.status_code == 200, first.text
        if finish == 'later_close':
            with factory() as db:
                receipt_id = db.scalar(select(IncomingReceiptItem.id))
            response = client.put(f'/api/incoming/receipt-items/{receipt_id}/accept-short', json={})
            total = 60
        else:
            delta = 41 if finish == 'over' else 39
            response = client.put(f'/api/incoming/receive/sr{item_id}', json=dict(
                received_quantity=delta, resolution_action=None if finish == 'over' else 'accept_short', idempotency_key='second'))
            total = 60 + delta
        assert response.status_code == 200, response.text
        pending = client.get('/api/incoming/pending').json()['items']
        assert not any(r['item_id'] == f'sr{item_id}' for r in pending)
        assert len(pending) == 1
        denied = client.put(f'/api/incoming/receive/sr{item_id}', json=dict(received_quantity=1, idempotency_key='closed'))
        assert denied.status_code == 409, denied.text
    with factory() as db:
        item = db.get(StockReplenishmentOrderItem, item_id)
        assert item.quantity == 100
        assert item.stocked_quantity == min(100, total)
        assert sum(l.quantity_available for l in db.scalars(select(InventoryLot))) == total
        prices = db.scalars(select(SupplierReceiptSettlementPriceFact)).all()
        assert sum(p.received_quantity_snapshot for p in prices) == total
        assert item.order.status == 'partially_stocked'
        rows = list_rows(db)
        assert len([r for r in rows if r['status'] == 'waiting']) == 1


def test_over_receipt_audit_failure_is_atomic(stock_replenishment_app, monkeypatch):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.warehouse_inventory import InventoryLot
    import app.services.incoming_receipts as service
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/requisition/stock-replenishment/orders', json=_customer_replenishment_payload(quantity=100))
        item_id = created.json()['items'][0]['id']
        original = service.append_audit_event
        def fail(*args, **kwargs):
            raise RuntimeError('injected audit failure')
        monkeypatch.setattr(service, 'append_audit_event', fail)
        payload = dict(received_quantity=101, idempotency_key='audit-fault')
        with pytest.raises(RuntimeError, match='injected audit failure'):
            client.put(f'/api/incoming/receive/sr{item_id}', json=payload)
        with factory() as db:
            assert db.get(StockReplenishmentOrderItem, item_id).stocked_quantity == 0
            assert not db.scalars(select(IncomingReceiptItem)).all()
            assert not db.scalars(select(InventoryLot)).all()
        monkeypatch.setattr(service, 'append_audit_event', original)
        retry = client.put(f'/api/incoming/receive/sr{item_id}', json=payload)
        assert retry.status_code == 200, retry.text

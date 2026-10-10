from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_replenishment_receipt_reversal import reversal_app, create_stock, receive, cancel_jobs, revert
from test_stock_replenishment_flow import stock_replenishment_app, _login, _confirm_replenishment_purchase
from app.models.procurement_source import ProcurementSourceLink
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
import pytest


def line(db, stock):
    link = db.scalar(select(ProcurementSourceLink).where(
        ProcurementSourceLink.stock_replenishment_item_id == stock['items'][0]['id']))
    return db.get(SupplierRequisitionOrderItem, link.supplier_item_id)


def test_selected_stock_line_releases_only_its_source_and_replay(reversal_app):
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        first, other = create_stock(client, 500), create_stock(client, 1560)
        _confirm_replenishment_purchase(client, {'items': first['items'] + other['items']})
        receipt = receive(client, first)
        with factory() as db:
            selected, sibling = line(db, first), line(db, other)
            selected_id, sibling_id, purchase_id = selected.id, sibling.id, selected.supplier_order_id
            payload = dict(expected_version=selected.version, idempotency_key=str(uuid4()), confirmed=True)
        url = f'/api/requisition/supplier-order-items/{selected_id}/void'
        blocked = client.put(url, json=payload)
        assert blocked.status_code == 409
        assert '来料' in blocked.text
        cancel_jobs(client, receipt['receipt_item_id'])
        assert revert(client, receipt['receipt_item_id']).status_code == 200
        result = client.put(url, json=payload)
        assert result.status_code == 200, result.text
        assert client.put(url, json=payload).json() == result.json()
        assert client.put(url, json=dict(payload, expected_version=99)).status_code == 409
        pending = client.get('/api/requisition/pending').json()['items']
        assert any(r.get('stock_replenishment_item_id') == first['items'][0]['id'] for r in pending)
        assert not any(r.get('stock_replenishment_item_id') == other['items'][0]['id'] for r in pending)
        unpurchased_receive = client.put(f"/api/incoming/receive/sr{first['items'][0]['id']}", json={
            'received_quantity': 500, 'idempotency_key': str(uuid4())})
        assert unpurchased_receive.status_code == 409
        replacement = _confirm_replenishment_purchase(client, first)
        assert replacement['created_orders']
        first['_test_received'] = 0
        replacement_receipt = receive(client, first)
        assert replacement_receipt['receipt_item_id'] != receipt['receipt_item_id']
    with factory() as db:
        purchase = db.get(SupplierRequisitionOrder, purchase_id)
        assert purchase.status == 'confirmed' and purchase.requisition_qty == 1560
        assert db.get(SupplierRequisitionOrderItem, sibling_id).status == 'active'
        links = list(db.scalars(select(ProcurementSourceLink)))
        assert [r.status for r in links if r.supplier_item_id == selected_id] == ['voided']
        assert [r.status for r in links if r.supplier_item_id == sibling_id] == ['active']
        assert sum(r.status == 'active' and r.stock_replenishment_item_id == first['items'][0]['id'] for r in links) == 1


def test_stock_line_audit_failure_and_stale_version_leave_source_bound(reversal_app, monkeypatch):
    app, factory = reversal_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        with factory() as db:
            selected = line(db, stock)
            item_id, version, order_id = selected.id, selected.version, selected.supplier_order_id
        url = f'/api/requisition/supplier-order-items/{item_id}/void'
        assert client.put(url, json=dict(expected_version=version+1, idempotency_key=str(uuid4()), confirmed=True)).status_code == 409
        from app.api import requisition as api
        monkeypatch.setattr(api, 'append_audit_event', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('audit fault')))
        assert client.put(url, json=dict(expected_version=version, idempotency_key=str(uuid4()), confirmed=True)).status_code == 500
    with factory() as db:
        assert line(db, stock).status == 'active'
        assert db.scalar(select(ProcurementSourceLink).where(ProcurementSourceLink.supplier_item_id == item_id)).status == 'active'
        assert db.get(SupplierRequisitionOrder, order_id).requisition_qty == 30


def test_stock_line_requires_admin_and_exact_single_source(reversal_app):
    from app.models.user import User
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        with factory() as db:
            selected = line(db, stock)
            item_id, version = selected.id, selected.version
            db.get(User, 1).role = 'boss'
            db.commit()
        url = f'/api/requisition/supplier-order-items/{item_id}/void'
        payload = dict(expected_version=version, idempotency_key=str(uuid4()), confirmed=True)
        assert client.put(url, json=payload).status_code == 403
        with factory() as db:
            db.get(User, 1).role = 'admin'
            db.get(SupplierRequisitionOrderItem, item_id).source_key = 'multiple_sources'
            db.commit()
        result = client.put(url, json=payload)
        assert result.status_code == 409
        assert result.json()['detail']['code'] == 'PROCUREMENT_SOURCE_SCOPE_REQUIRED'


def test_receipt_waiting_for_source_cannot_use_withdrawn_purchase_link(reversal_app, monkeypatch):
    """Withdraw between the preflight link read and the source row claim."""
    from sqlalchemy.sql.dml import Update
    from app.services.incoming_receipts import _stock_target, IncomingReceiptError
    app, factory = reversal_app
    with TestClient(app) as client:
        _login(client)
        stock = create_stock(client)
        _confirm_replenishment_purchase(client, stock)
        with factory() as db:
            selected = line(db, stock)
            item_id, version = selected.id, selected.version
        with factory() as receiving_db:
            original = receiving_db.execute
            withdrew = []

            def interleave(statement, *args, **kwargs):
                if isinstance(statement, Update) and statement.table.name == 'stock_replenishment_orders' and not withdrew:
                    withdrew.append(True)
                    result = client.put(f'/api/requisition/supplier-order-items/{item_id}/void', json=dict(
                        expected_version=version, idempotency_key=str(uuid4()), confirmed=True))
                    assert result.status_code == 200, result.text
                return original(statement, *args, **kwargs)

            monkeypatch.setattr(receiving_db, 'execute', interleave)
            with pytest.raises(IncomingReceiptError, match='有效采购单'):
                _stock_target(receiving_db, f"sr{stock['items'][0]['id']}", claim_for_receipt=True)

"""Customer demand reductions on disposable factory copies only."""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select, text
from app.models.order import OrderItem
from app.models.delivery import Delivery, DeliveryItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_bom_other_products_acceptance import factory_http


URL = '/api/orders/items/10147/quantity'


def payload(client, quantity=300, key='isolated-quantity-300'):
    state = client.get(URL)
    assert state.status_code == 200, state.text
    state = state.json()
    return dict(quantity=quantity, expected_quantity=state['quantity'],
                expected_revision=state['expected_revision'], idempotency_key=key)


def frozen(db):
    # Full historical rows, including frozen prices, quantities and revisions.
    return {table: db.execute(text(f'SELECT * FROM {table} ORDER BY id')).all()
            for table in ('production_completions', 'production_tasks',
                          'sales_deliveries', 'sales_delivery_items',
                          'delivery_inventory_allocations')}


def balances(db):
    lot = db.get(InventoryLot, 919)
    return (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed, lot.version)


def test_delivered_600_to_300_releases_only_remaining_stock_and_preserves_history(factory_http):
    client, db = factory_http
    old = frozen(db); stock = balances(db)
    body = payload(client)
    result = client.put(URL, json=body)
    assert result.status_code == 200, result.text
    db.expire_all()
    item = db.get(OrderItem, 10147)
    assert (item.quantity, item.delivered_quantity, item.unit_price, item.subtotal) == (
        300, 300, Decimal('2.56'), Decimal('768'))
    assert balances(db) == (stock[0]+300, stock[1]-300, stock[2], stock[3]+1)
    reservation = db.get(InventoryReservation, 704)
    assert (reservation.reserved_stock_quantity, reservation.consumed_stock_quantity,
            reservation.released_stock_quantity) == (600, 300, 300)
    assert frozen(db) == old
    replay = client.put(URL, json=body)
    assert replay.status_code == 200 and replay.json()['replayed']
    assert client.put(URL, json={**body, 'quantity': 400}).status_code == 409
    assert client.put(URL, json={**body, 'idempotency_key': 'stale-new-key'}).status_code == 409
    db.expire_all()
    assert balances(db)[0] == stock[0]+300
    assert frozen(db) == old


def test_partial_reduction_preserves_remaining_demand_and_rejects_under_delivery(factory_http):
    client, db = factory_http
    before = balances(db)
    invalid = client.put(URL, json=payload(client, 299))
    assert invalid.status_code == 409 and '最低300' in invalid.text
    good = client.put(URL, json=payload(client, 450, 'isolated-partial-450'))
    assert good.status_code == 200, good.text
    db.expire_all()
    assert balances(db) == (before[0]+150, before[1]-150, before[2], before[3]+1)
    assert db.get(InventoryReservation,704).released_stock_quantity == 150
    assert client.put(URL, json=payload(client,300,'isolated-second-300')).status_code == 200
    db.expire_all()
    assert balances(db)[0] == before[0]+300


def test_pending_delivery_current_revision_counts_but_voided_does_not(factory_http):
    client, db = factory_http
    pending = Delivery(customer_id=136, delivery_number='ISOLATED-PENDING-QTY',
                       delivery_date=date(2026,9,28), status='pending')
    db.add(pending); db.flush()
    db.add(DeliveryItem(delivery_id=pending.id, order_item_id=10147, delivered_quantity=100))
    db.commit()
    state = client.get(URL).json()
    assert state['minimum_quantity'] == 400
    assert client.put(URL,json=payload(client,300)).status_code == 409
    pending.status='voided'; db.commit()
    assert client.get(URL).json()['minimum_quantity'] == 300
    assert client.put(URL,json=payload(client,300)).status_code == 200


def test_audit_failure_rolls_back_quantity_stock_amount_and_demand(factory_http, monkeypatch):
    from app.api import orders
    client,db=factory_http
    before=balances(db); history=frozen(db)
    def fail(*args,**kwargs): raise RuntimeError('isolated quantity audit failure')
    monkeypatch.setattr(orders,'_append_order_audit',fail)
    with pytest.raises(RuntimeError,match='isolated quantity audit failure'):
        client.put(URL,json=payload(client))
    db.expire_all()
    assert db.get(OrderItem,10147).quantity==600
    assert db.get(OrderItem,10147).subtotal==Decimal('1536')
    assert balances(db)==before and frozen(db)==history


def test_permission_customer_scope_and_strict_payload(factory_http, monkeypatch):
    from app.api import orders
    from fastapi import HTTPException
    client,db=factory_http
    body=payload(client)
    assert client.put(URL,json={**body,'unit_price':1}).status_code==422
    assert client.put(URL,json={**body,'quantity':300.5}).status_code==422
    def deny(*args,**kwargs): raise HTTPException(403,'客户范围受限')
    monkeypatch.setattr(orders,'require_customer_access',deny)
    assert client.get(URL).status_code==403
    assert client.put(URL,json=body).status_code==403
    client.cookies.clear()
    assert client.get(URL).status_code==401
    assert client.put(URL,json=body).status_code==401

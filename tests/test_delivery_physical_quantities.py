"""Customer units must never substitute for physical warehouse pieces."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.delivery import DeliveryItem
from app.models.product import Product
from app.models.material import Material
from app.models.warehouse_inventory import InventoryLot
from app.services.delivery_quantities import (
    available_customer_quantity, product_basis, physical_stock_basis,
)
from tests.test_p1_15b_unordered_finished_delivery import (
    unordered_finished_delivery_app, _add_finished, _login, _short_receipt_payload,
)


def seed_physical(factory, quantity, *, external=True):
    # Establish an existing stock fixture before changing its product profile.
    # Receiving/cost conversion is a separate integration contract; this suite
    # exercises dispatch against a batch whose physical basis is already verified.
    with factory() as db:
        product = db.scalar(select(Product).where(Product.product_code == 'P115B-BOX-A'))
        material = Material(code='DUAL-TEST', supplier_name='测试供应商',
                            quote_price=2, price_unit='元/㎡', purchase_currency='CNY',
                            purchase_tax_included=True, layer_count=3)
        db.add(material)
        db.flush()
        product.material_id = material.id
        product.report_length_mm = 800
        product.report_width_mm = 180
        product.pieces_per_box = 1
        db.commit()
        customer_id, product_id = product.customer_id, product.id
    lot_id, _ = _add_finished(factory, customer_id=customer_id, product_id=product_id,
                              quantity=quantity, key='physical-fixture')
    with factory() as db:
        product = db.scalar(select(Product).where(Product.product_code == 'P115B-BOX-A'))
        if not external:
            return customer_id, product_id, lot_id, product_basis(product)
        product.unit = '只'
        product.supply_mode = 'external_purchase'
        product.external_packaging_category_code = 'honeycomb_board'
        product.external_packaging_specification_json = '{}'
        product.external_packaging_specification_summary = '测试实物'
        product.external_packaging_candidate_snapshot_json = '{}'
        product.external_packaging_purchase_unit = '片'
        product.external_packaging_default_order_quantity_basis = 1
        product.external_packaging_default_purchase_quantity_basis = 2
        db.commit()
        customer_id, product_id = product.customer_id, product.id
        basis = product_basis(product)
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        lot.finished_detail.physical_basis_json = physical_stock_basis(
            lot.finished_detail.physical_basis_json, basis)
        db.commit()
    return customer_id, product_id, lot_id, basis


def payload(customer_id, product_id, lot_id, customer_quantity):
    return dict(customer_id=customer_id, delivery_date='2026-07-29',
                source_mode='unordered_finished', lines=[dict(
                    source_type='finished_stock', product_id=product_id,
                    delivered_quantity=customer_quantity, unit_price='3.60',
                    allocations=[dict(inventory_lot_id=lot_id, quantity=customer_quantity * 2)])])


@pytest.mark.parametrize('customer_quantity,remaining', [(100, 0), (50, 100)])
def test_physical_dispatch_cancel_and_frozen_draft(unordered_finished_delivery_app,
                                                  customer_quantity, remaining):
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    request = payload(customer_id, product_id, lot_id, customer_quantity)
    with TestClient(app) as client:
        _login(client)
        candidates = client.get('/api/deliveries/unordered-finished-candidates', params={'customer_id': customer_id})
        assert candidates.status_code == 200, candidates.text
        candidate = next(row for row in candidates.json()['items'] if row['inventory_lot_id'] == lot_id)
        assert candidate['available_quantity'] == 200
        assert candidate['available_customer_quantity'] == 100
        assert candidate['quantity_contract']['physical_basis'] == 2
        created = client.post('/api/deliveries', json=request)
        assert created.status_code == 201, created.text
        delivery_id = created.json()['id']
        goods = created.json()['items'][0]['actual_goods_lines'][0]
        assert goods['quantity'] == customer_quantity * 2
        assert goods['unit'] == '片'
        printed = client.get(f'/api/deliveries/{delivery_id}/print')
        assert printed.status_code == 200, printed.text
        printed_line = printed.json()['items'][0]
        assert printed_line['quantity'] == customer_quantity
        assert printed_line['unit'] == '只'
        assert printed_line['actual_goods_quantity'] == customer_quantity * 2
        with factory() as db:
            assert db.get(InventoryLot, lot_id).quantity_available == 200
            product = db.get(Product, product_id)
            product.external_packaging_default_purchase_quantity_basis = 3
            db.commit()
        edited = client.put(f'/api/deliveries/{delivery_id}', json=request)
        assert edited.status_code == 200, edited.text
        with factory() as db:
            item = db.scalar(select(DeliveryItem).where(
                DeliveryItem.delivery_id == delivery_id, DeliveryItem.is_current.is_(True)))
            frozen = json.loads(item.quantity_contract_json)
            assert frozen['physical_quantity'] == customer_quantity * 2
            assert item.delivered_quantity == customer_quantity
        dispatched = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        client.put(f'/api/deliveries/{delivery_id}/dispatch')
        with factory() as db:
            assert db.get(InventoryLot, lot_id).quantity_available == remaining
        cancelled = client.put(f'/api/deliveries/{delivery_id}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        client.put(f'/api/deliveries/{delivery_id}/cancel')
        with factory() as db:
            assert db.get(InventoryLot, lot_id).quantity_available == 200


def test_dispatch_stock_warning_reads_real_consume_and_exact_threshold(unordered_finished_delivery_app):
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    with factory() as db:
        policy = InventoryStockPolicy(policy_name='发货阈值测试', target_inventory_type='finished',
            customer_id=customer_id, product_id=product_id, warning_quantity=100,
            target_quantity=200, active=True)
        db.add(policy)
        db.commit()
        policy_id = policy.id
        initial_drafts = list(db.scalars(select(StockReplenishmentOrder.id)))
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 50))
        assert created.status_code == 201, created.text
        delivery_id = created.json()['id']
        endpoint = f'/api/deliveries/{delivery_id}/stock-warnings'
        before = client.get(endpoint)
        assert before.status_code == 200 and before.json()['items'] == [], before.text
        sent = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert sent.status_code == 200, sent.text
        warning = client.get(endpoint)
        assert warning.status_code == 200, warning.text
        assert warning.json()['items'] == [{
            'id': policy_id, 'product_id': product_id, 'customer_id': customer_id,
            'product_code': created.json()['items'][0]['product_code'],
            'product_name': created.json()['items'][0]['product_name'],
            'available_quantity': 100, 'warning_quantity': 100, 'target_quantity': 200,
            'incoming_physical_quantity': 0, 'pending_physical_quantity': 0, 'suggested_physical_quantity': 100,
            'physical_unit': '片',
        }]
        assert client.get(endpoint).json() == warning.json()
        cancelled = client.put(f'/api/deliveries/{delivery_id}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        assert client.get(endpoint).json()['items'] == []
    with factory() as db:
        assert list(db.scalars(select(StockReplenishmentOrder.id))) == initial_drafts
        assert db.get(InventoryLot, lot_id).quantity_available == 200


def test_odd_physical_stock_cannot_supply_full_customer_quantity(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, basis = seed_physical(factory, 199)
    assert available_customer_quantity(basis, 199) == 99
    with TestClient(app) as client:
        _login(client)
        rejected = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 100))
        assert rejected.status_code in (409, 422), rejected.text
        accepted = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 99))
        assert accepted.status_code == 201, accepted.text
        dispatched = client.put(f"/api/deliveries/{accepted.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
    with factory() as db:
        assert db.get(InventoryLot, lot_id).quantity_available == 1


def test_two_partial_shipments_consume_physical_stock_once(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    with TestClient(app) as client:
        _login(client)
        for remaining in (100, 0):
            created = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 50))
            assert created.status_code == 201, created.text
            dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")
            assert dispatched.status_code == 200, dispatched.text
            with factory() as db:
                assert db.get(InventoryLot, lot_id).quantity_available == remaining


def test_customer_short_receipt_returns_physical_pieces(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 100))
        assert created.status_code == 201, created.text
        delivery_id = created.json()['id']
        dispatched = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            item_id = db.scalar(select(DeliveryItem.id).where(
                DeliveryItem.delivery_id == delivery_id, DeliveryItem.is_current.is_(True)))
        receipt = client.post('/api/finance/return_receipts',
                              json=_short_receipt_payload(delivery_id, item_id, quantity=90))
        assert receipt.status_code == 201, receipt.text
        with factory() as db:
            assert db.get(InventoryLot, lot_id).quantity_available == 20
            assert db.get(DeliveryItem, item_id).delivered_quantity == 100
        cancelled = client.post(f"/api/finance/return_receipts/{receipt.json()['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.get(InventoryLot, lot_id).quantity_available == 0


def test_mobile_pick_uses_physical_quantity_and_rejects_half_customer_unit(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    customer_id, product_id, lot_id, _ = seed_physical(factory, 200)
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/deliveries', json=payload(customer_id, product_id, lot_id, 100))
        assert created.status_code == 201, created.text
        delivery_id = created.json()['id']
        pushed = client.post(f'/api/deliveries/{delivery_id}/pick-task')
        assert pushed.status_code == 201, pushed.text
        task = pushed.json()
        row = task['items'][0]
        assert row['original_quantity'] == 200
        assert sum(line['pick_quantity'] for line in row['location_lines']) == 200
        assert {line['unit'] for line in row['location_lines']} == {'片'}
        endpoint = f"/api/delivery-picks/{task['id']}/items/{row['id']}"
        odd = client.put(endpoint, json=dict(pick_status='partial', picked_quantity=99))
        assert odd.status_code == 422, odd.text
        picked = client.put(endpoint, json=dict(pick_status='partial', picked_quantity=100))
        assert picked.status_code == 200, picked.text
        submitted = client.post(f"/api/delivery-picks/{task['id']}/submit")
        assert submitted.status_code == 200, submitted.text
        applied = client.post(f"/api/delivery-picks/{task['id']}/apply")
        assert applied.status_code == 200, applied.text
        with factory() as db:
            item = db.scalar(select(DeliveryItem).where(
                DeliveryItem.delivery_id == delivery_id, DeliveryItem.is_current.is_(True)))
            assert item.delivered_quantity == 50
            assert json.loads(item.quantity_contract_json)['physical_quantity'] == 100
            assert db.get(InventoryLot, lot_id).quantity_available == 200
        dispatched = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            assert db.get(InventoryLot, lot_id).quantity_available == 100

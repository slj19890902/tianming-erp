from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select, func
import pytest

from test_p1_40b_external_packaging_routing import routing_app, p1_40a_app, _login, _seed_price, _order_payload_for
from test_p1_40a_packaging_masterdata import _external_payload
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


def prepare(app, client, ratio='1'):
    ids = app.state.fixture
    _login(client, 'p1-40a-admin')
    product = _external_payload(ids, candidates=[{'external_product_id':ids['CG-870-A'], 'is_default':True}])
    product['external_packaging_default_purchase_quantity_basis'] = ratio
    p = client.post('/api/master/products', json=product)
    assert p.status_code == 201, p.text
    _seed_price(app, ids['CG-870-A'])
    payload = _order_payload_for(p.json()['id'], ids['customer_a'])
    payload['items'][0]['quantity'] = 1000
    payload['items'][0]['external_packaging_purchase_quantity_basis'] = '1'
    o = client.post('/api/orders', json=payload)
    assert o.status_code == 201, o.text
    oid = o.json()['id']
    _seed_material_and_staging(app.state.factory)
    preview = client.get(f'/api/orders/{oid}/external-packaging-purchase').json()
    result = client.post(f'/api/orders/{oid}/external-packaging-purchase/confirm', json={
        'idempotency_key':'direct-purchase', 'lines':[dict(order_component_id=r['order_component_id'],
        candidate_id=r['default_candidate_id'], purchase_quantity=r['suggested_purchase_quantity']) for r in preview['items']]})
    assert result.status_code == 200, result.text
    pending = client.get('/api/external-packaging-purchases/pending-receipts').json()['purchase_orders'][0]
    return oid, pending['id'], pending['items'][0]['purchase_item_id']


def receive(client, pid, line, qty, key='direct-receipt'):
    return client.post(f'/api/external-packaging-purchases/{pid}/receipts', json={
        'idempotency_key':key, 'lines':[{'purchase_item_id':line, 'received_quantity':str(qty)}]})


def test_direct_receipt_posts_real_finished_and_idempotent(routing_app):
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.models.production import ProductionTask
    from app.api.deliveries import _delivery_remaining_quantity
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client)
        r = receive(client, pid, line, 1000)
        assert r.status_code == 200, r.text
        assert r.json()['receipt']['items'][0]['converted_finished_quantity'] == 1000
        again = receive(client, pid, line, 1000)
        assert again.status_code == 200 and not again.json()['created']
        assert receive(client, pid, line, 999).status_code == 409
    with routing_app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
        lot = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == 'direct_external_receipt'))
        assert lot.finished_detail.product_id == item.product_id
        assert lot.quantity_reserved == 1000 and lot.quantity_available == 0
        assert db.scalar(select(func.count()).select_from(ProductionTask)) == 0
        assert _delivery_remaining_quantity(db, item) == 1000
        from datetime import date
        from app.models.delivery import Delivery, DeliveryItem
        from app.services.semi_finished_inventory import consume_delivery_item_inventory, reverse_delivery_item_inventory
        delivery = Delivery(delivery_number='DIRECT-TEST', customer_id=lot.finished_detail.owner_customer_id,
            delivery_date=date.today(), status='dispatched')
        db.add(delivery)
        db.flush()
        line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id, delivered_quantity=1000)
        db.add(line)
        db.flush()
        consume_delivery_item_inventory(db, delivery_item_id=line.id, delivered_quantity_after_dispatch=1000,
            operator_id=None, operation_key='direct-dispatch')
        item.delivered_quantity = 1000
        db.flush()
        db.refresh(lot)
        assert lot.quantity_consumed == 1000 and lot.quantity_reserved == 0
        reverse_delivery_item_inventory(db, delivery_item_id=line.id, delivered_quantity_after_cancel=800,
            operator_id=None, operation_key='direct-revise')
        item.delivered_quantity = 800
        db.flush()
        db.refresh(lot)
        assert lot.quantity_consumed == 800 and lot.quantity_reserved == 200
        assert lot.warehouse_location_id is not None
        assert _delivery_remaining_quantity(db, item) == 200


def test_split_receipt_carries_fraction_and_uses_frozen_ratio(routing_app):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client, ratio='3')
        a = receive(client, pid, line, 2, 'fraction-1')
        assert a.status_code == 200, a.text
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            db.get(Product, item.product_id).external_packaging_default_purchase_quantity_basis = Decimal(7)
            db.commit()
        b = receive(client, pid, line, 4, 'fraction-2')
        assert b.status_code == 200, b.text
        assert b.json()['receipt']['items'][0]['converted_finished_quantity'] == 2
    with routing_app.state.factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == 'direct_external_receipt'))
        assert lot.quantity_reserved == 2


def test_coated_board_keeps_receipt_only(routing_app):
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client)
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            item.external_packaging_category_code_snapshot = 'coated_board'
            db.commit()
        r = receive(client, pid, line, 1000)
        assert r.status_code == 200, r.text
    with routing_app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0


def test_receipt_reversal_restores_pending_without_deleting_facts(routing_app):
    from app.models.warehouse_inventory import InventoryLot
    from app.models.external_packaging_purchase import ExternalPackagingReceipt
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client)
        first = receive(client, pid, line, 1000)
        assert first.status_code == 200, first.text
        with routing_app.state.factory() as db:
            rid = db.scalar(select(ExternalPackagingReceipt.id))
        payload = dict(idempotency_key='direct-reverse', reason='测试撤销', confirmed=True)
        r = client.post(f'/api/external-packaging-receipts/{rid}/reverse', json=payload)
        assert r.status_code == 200, r.text
        again = client.post(f'/api/external-packaging-receipts/{rid}/reverse', json=payload)
        assert again.status_code == 200 and not again.json()['created'], again.text
    with routing_app.state.factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == 'direct_external_receipt'))
        assert lot.status == 'closed' and lot.quantity_reserved == 0 and lot.quantity_available == 0
        assert db.scalar(select(func.count()).select_from(ExternalPackagingReceipt)) == 1


def test_failure_rolls_back_receipt_and_lot(routing_app, monkeypatch):
    from app.models.external_packaging_purchase import ExternalPackagingReceipt
    from app.models.warehouse_inventory import InventoryLot
    from app.services import production_workflow
    def fail(*args, **kwargs):
        raise production_workflow.ProductionWorkflowError('测试库存预占冲突', 409)
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client)
        monkeypatch.setattr(production_workflow, '_reserve_component_completion_lot', fail)
        r = receive(client, pid, line, 1000)
        assert r.status_code == 409, r.text
    with routing_app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(ExternalPackagingReceipt)) == 0
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0


def test_existing_receipt_only_history_is_not_reentered(routing_app):
    from app.models.external_packaging_purchase import ExternalPackagingReceipt, ExternalPackagingReceiptItem
    from app.models.warehouse_inventory import InventoryLot
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client)
        with routing_app.state.factory() as db:
            old = ExternalPackagingReceipt(purchase_order_id=pid, receipt_number='OLD',
                idempotency_key='old', request_fingerprint='0'*64, received_by=1)
            db.add(old)
            db.flush()
            db.add(ExternalPackagingReceiptItem(receipt_id=old.id, purchase_item_id=line,
                received_quantity=100, purchase_unit_snapshot='根', converted_finished_quantity=0))
            db.commit()
        response = receive(client, pid, line, 900)
        assert response.status_code == 409 and '历史' in response.text
    with routing_app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(ExternalPackagingReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0


def test_customer_scope_denied_has_no_receipt(routing_app):
    from app.models.external_packaging_purchase import ExternalPackagingReceipt
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client)
        _login(client, 'p1-40b-scoped')
        response = receive(client, pid, line, 1000)
        assert response.status_code == 403, response.text
    with routing_app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(ExternalPackagingReceipt)) == 0

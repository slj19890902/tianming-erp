"""Actual replenishment receipt -> physical stock -> unordered customer delivery."""
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from test_p1_140_external_stock_replenishment import external_stock_app, p1_40a_app, _seed_external_warning, _login
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, InventoryMovement, WarehouseLocation
from app.models.order import Order


def test_received_200_pieces_can_dispatch_customer_100_without_order(external_stock_app):
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.core.inventory_entry_guard import validate_entries
    from app.services.warehouse_display_units import lot_display_unit
    external_stock_app.include_router(router, prefix='/api/deliveries')
    policy_id, product_id = _seed_external_warning(external_stock_app)
    with external_stock_app.state.factory() as db:
        db.add(WarehouseLocation(location_code='F1-DISPATCH-01', location_name='一楼成品待送区',
            warehouse_type='finished', warehouse_floor=1, area_code='DISPATCH',
            storage_type='temporary_aisle', source_version='P1-25C', placement_status='placed', is_active=True))
        db.commit()
    with TestClient(external_stock_app) as client:
        _login(client)
        draft = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft').json()
        line = draft['items'][0]
        line['external_purchase_quantity'] = '200'
        order = client.post('/api/requisition/stock-replenishment/orders', json=dict(
            source_type='stock_warning', idempotency_key='physical-e2e-stock-order',
            customer_id=draft['customer_id'], supplier_name=draft['supplier_name'], stock_now=False, items=[line]))
        assert order.status_code == 201, order.text
        purchase = order.json()['external_purchase_orders'][0]
        received = client.post(f"/api/external-packaging-purchases/{purchase['id']}/receipts", json=dict(
            idempotency_key='physical-e2e-receipt', lines=[dict(
                purchase_item_id=purchase['items'][0]['id'], received_quantity='200')]))
        assert received.status_code == 200, received.text
        with external_stock_app.state.factory() as db:
            lot = db.scalar(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
                FinishedGoodsInventoryDetail.product_id == product_id))
            lot_id = lot.id
            assert lot.quantity_available == 200
            assert lot_display_unit(lot) == '片'
            assert lot.estimated_unit_cost_snapshot == Decimal('8.5000')
            db.info['new_inventory_entry_ids'] = {lot_id}
            validate_entries(db)
            assert db.scalar(select(func.count(Order.id))) == 0
        candidates = client.get('/api/deliveries/unordered-finished-candidates', params={'customer_id': draft['customer_id']})
        assert candidates.status_code == 200, candidates.text
        row = next(x for x in candidates.json()['items'] if x['inventory_lot_id'] == lot_id)
        assert row['available_customer_quantity'] == 100
        delivery = client.post('/api/deliveries', json=dict(customer_id=draft['customer_id'],
            delivery_date=str(beijing_today()), source_mode='unordered_finished', lines=[dict(
                source_type='finished_stock', product_id=product_id, delivered_quantity=100,
                unit_price='20', allocations=[dict(inventory_lot_id=lot_id, quantity=200)])]))
        assert delivery.status_code == 201, delivery.text
        delivery_id = delivery.json()['id']
        dispatched = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        with external_stock_app.state.factory() as db:
            lot = db.get(InventoryLot, lot_id)
            assert lot.quantity_available == 0 and lot.quantity_consumed == 200
            assert db.scalar(select(func.count(Order.id))) == 0
            assert db.scalar(select(func.sum(InventoryMovement.quantity)).where(
                InventoryMovement.inventory_lot_id == lot_id, InventoryMovement.movement_type == 'consume')) == 200

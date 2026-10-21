"""Actual replenishment receipt -> physical stock -> unordered customer delivery."""
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from test_p1_140_external_stock_replenishment import external_stock_app, p1_40a_app, _seed_external_warning, _login
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, InventoryMovement
from app.models.order import Order


def test_received_200_pieces_can_dispatch_customer_100_without_order(external_stock_app, monkeypatch):
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.core.inventory_entry_guard import validate_entries
    from app.services.warehouse_display_units import lot_display_unit
    from app.models.user import User
    from test_p1_134_production_storage_routing import _seed_formal_location, _patch_floor_identity
    external_stock_app.include_router(router, prefix='/api/deliveries')
    policy_id, product_id = _seed_external_warning(external_stock_app)
    with external_stock_app.state.factory() as db:
        # Current receipt routing requires a published location in the synthetic map.
        actor = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
        _seed_formal_location(db, floor_number=1, area_code='FIN-001',
            location_code='F1-DISPATCH-01', map_revision='external-physical-test',
            map_feature_id='external-physical-fin', storage_type='ground', actor_id=actor.id,
            with_ground_plan=True)
        db.commit()
    _patch_floor_identity(monkeypatch, floor_number=1, revision='external-physical-test',
                          features={'external-physical-fin': 'FIN-001'})
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
        from test_delivery_dispatch_commands import command
        dispatched = client.put(f'/api/deliveries/{delivery_id}/dispatch',
                                json=command(client, delivery_id, 'external-physical-dispatch'))
        assert dispatched.status_code == 200, dispatched.text

        # Editing only the common-box customer wording refreshes this existing
        # delivery, including actual-goods rows, while retaining its frozen view.
        from test_p1_40a_packaging_masterdata import _honeycomb_payload
        print_url = f'/api/deliveries/{delivery_id}/print?show_prices=true'
        before_print = client.get(print_url)
        assert before_print.status_code == 200, before_print.text
        before_print = before_print.json()
        frozen_before_response = client.get(print_url + '&description_mode=original')
        assert frozen_before_response.status_code == 200, frozen_before_response.text
        frozen_before = frozen_before_response.json()
        product_url = f'/api/master/products/{product_id}'
        master = client.get(product_url).json()
        update = dict(_honeycomb_payload(external_stock_app.state.fixture),
                      expected_version=master['version'], customer_specification='客户蜂窝板 80×18×6cm')
        preview = client.post(product_url + '/update-preview', json=update)
        assert preview.status_code == 200, preview.text
        update['confirmation_token'] = preview.json()['confirmation_token']
        saved = client.put(product_url, json=update)
        assert saved.status_code == 200, saved.text
        refreshed = client.get(print_url).json()
        assert refreshed['customer_document_rows'][0]['specification'] == '客户蜂窝板 80×18×6cm'
        assert refreshed['customer_document_rows'][0]['quantity'] == before_print['customer_document_rows'][0]['quantity']
        assert refreshed['customer_document_rows'][0]['unit_price'] == before_print['customer_document_rows'][0]['unit_price']
        assert refreshed['total_amount'] == before_print['total_amount']
        frozen_after_response = client.get(print_url + '&description_mode=original')
        assert frozen_after_response.status_code == 200, frozen_after_response.text
        frozen_after = frozen_after_response.json()
        # A legacy default template keeps its original `items` representation.
        assert frozen_after['items'] == frozen_before['items']
        stale = client.post(f'/api/deliveries/{delivery_id}/customer-print-events', json=dict(
            idempotency_key='external-customer-spec-stale', document_hash=before_print['document_hash'],
            show_prices=True, description_mode='current', order_context=''))
        assert stale.status_code == 409, stale.text
        printed = client.post(f'/api/deliveries/{delivery_id}/customer-print-events', json=dict(
            idempotency_key='external-customer-spec-current', document_hash=refreshed['document_hash'],
            show_prices=True, description_mode='current', order_context=''))
        assert printed.status_code == 200, printed.text
        with external_stock_app.state.factory() as db:
            lot = db.get(InventoryLot, lot_id)
            assert lot.quantity_available == 0 and lot.quantity_consumed == 200
            assert lot.estimated_unit_cost_snapshot == Decimal('8.5000')
            assert db.scalar(select(func.count(Order.id))) == 0
            assert db.scalar(select(func.sum(InventoryMovement.quantity)).where(
                InventoryMovement.inventory_lot_id == lot_id, InventoryMovement.movement_type == 'consume')) == 200

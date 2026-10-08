from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.core.time_contract import beijing_today
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


@pytest.mark.parametrize('physical_ratio', [1, 2])
def test_direct_receipt_posts_real_finished_and_idempotent(routing_app, physical_ratio):
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.models.production import ProductionTask
    from app.api.deliveries import _delivery_remaining_quantity
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client, ratio=str(physical_ratio))
        r = receive(client, pid, line, 1000 * physical_ratio)
        assert r.status_code == 200, r.text
        assert r.json()['receipt']['items'][0]['converted_finished_quantity'] == 1000
        again = receive(client, pid, line, 1000 * physical_ratio)
        assert again.status_code == 200 and not again.json()['created']
        assert receive(client, pid, line, 999).status_code == 409
    with routing_app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
        lot = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == 'direct_external_receipt'))
        assert lot.finished_detail.product_id == item.product_id
        assert lot.quantity_reserved == 1000 * physical_ratio and lot.quantity_available == 0
        reservation = db.scalar(select(InventoryReservation).where(InventoryReservation.inventory_lot_id == lot.id))
        from app.services.delivery_quantities import requirement_amount
        assert requirement_amount(reservation, 'credited_requirement_quantity') == 1000
        from app.services.inventory_valuation import cost_payload
        cost = cost_payload(lot, db)
        assert Decimal(cost['unit_cost']) == lot.estimated_unit_cost_snapshot
        assert cost['display_unit'] == item.external_packaging_purchase_unit_snapshot
        from app.core.inventory_entry_guard import validate_entries
        db.info['new_inventory_entry_ids'] = {lot.id}
        validate_entries(db)
        from app.models.user import User
        operator_id = db.scalar(select(User.id).where(User.username == 'p1-40a-admin'))
        assert db.scalar(select(func.count()).select_from(ProductionTask)) == 0
        assert _delivery_remaining_quantity(db, item) == 1000
        from datetime import date
        from app.models.delivery import Delivery, DeliveryItem
        from app.services.semi_finished_inventory import consume_delivery_item_inventory, reverse_delivery_item_inventory
        delivery = Delivery(delivery_number='DIRECT-TEST', customer_id=lot.finished_detail.owner_customer_id,
            delivery_date=date.today(), status='dispatched')
        db.add(delivery)
        db.flush()
        from app.services.delivery_snapshots import build_order_delivery_snapshot
        from app.services.delivery_quantities import decode
        line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id, delivered_quantity=1000,
            **build_order_delivery_snapshot(db, item, 1000))
        assert decode(line.quantity_contract_json)['physical_quantity'] == 1000 * physical_ratio
        db.add(line)
        db.flush()
        from app.services.order_stock_reference import stock_reference
        reference_args=dict(customer_id=delivery.customer_id,product_id=item.product_id,
            product_code=item.snapshot_product_code,quantity_unit=item.external_packaging_purchase_unit_snapshot,
            remaining_quantity=1000*physical_ratio)
        assert stock_reference(db,**reference_args)['status']=='green'
        contract=line.quantity_contract_json;line.quantity_contract_json=None;db.flush()
        assert stock_reference(db,**reference_args)['status']=='unknown'
        line.quantity_contract_json=contract;db.flush()
        consume_delivery_item_inventory(db, delivery_item_id=line.id, delivered_quantity_after_dispatch=1000,
            operator_id=operator_id, operation_key='direct-dispatch')
        item.delivered_quantity = 1000
        db.flush()
        db.refresh(lot)
        assert lot.quantity_consumed == 1000 * physical_ratio and lot.quantity_reserved == 0
        reverse_delivery_item_inventory(db, delivery_item_id=line.id, delivered_quantity_after_cancel=800,
            operator_id=operator_id, operation_key='direct-revise')
        item.delivered_quantity = 800
        db.flush()
        db.refresh(lot)
        assert lot.quantity_consumed == 800 * physical_ratio and lot.quantity_reserved == 200 * physical_ratio
        assert lot.warehouse_location_id is not None
        assert _delivery_remaining_quantity(db, item) == 200


@pytest.mark.parametrize('ratio,receipts', [(3, [2, 4]), (2, [1, 1, 1, 1])])
def test_split_receipt_carries_fraction_and_uses_frozen_ratio(routing_app, ratio, receipts):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client, ratio=str(ratio))
        a = receive(client, pid, line, receipts[0], 'fraction-1')
        assert a.status_code == 200, a.text
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            from app.api.deliveries import _delivery_remaining_quantity
            assert _delivery_remaining_quantity(db, item) == 0
            db.get(Product, item.product_id).external_packaging_default_purchase_quantity_basis = Decimal(7)
            db.commit()
        for index, quantity in enumerate(receipts[1:], 2):
            b = receive(client, pid, line, quantity, f'fraction-{index}')
            assert b.status_code == 200, b.text
    with routing_app.state.factory() as db:
        lots = list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type == 'direct_external_receipt')))
        assert sum(lot.quantity_available + lot.quantity_reserved for lot in lots) == sum(receipts)
        assert sum(lot.quantity_reserved for lot in lots) == sum(receipts)
        from app.api.deliveries import _delivery_remaining_quantity
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
        assert _delivery_remaining_quantity(db, item) == 2
        from datetime import date
        from app.models.delivery import Delivery, DeliveryItem
        from app.models.user import User
        from app.services.delivery_snapshots import build_order_delivery_snapshot
        from app.services.semi_finished_inventory import consume_delivery_item_inventory, reverse_delivery_item_inventory
        operator_id = db.scalar(select(User.id).where(User.username == 'p1-40a-admin'))
        delivery = Delivery(delivery_number='FRACTION-TEST', customer_id=lots[0].finished_detail.owner_customer_id,
            delivery_date=date.today(), status='dispatched')
        db.add(delivery)
        db.flush()
        delivery_line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id, delivered_quantity=2,
            **build_order_delivery_snapshot(db, item, 2))
        db.add(delivery_line)
        db.flush()
        consume_delivery_item_inventory(db, delivery_item_id=delivery_line.id, delivered_quantity_after_dispatch=2,
            operator_id=operator_id, operation_key='fraction-dispatch')
        item.delivered_quantity = 2
        db.flush()
        for lot in lots:
            db.refresh(lot)
        assert sum(lot.quantity_consumed for lot in lots) == sum(receipts)
        assert _delivery_remaining_quantity(db, item) == 0
        reverse_delivery_item_inventory(db, delivery_item_id=delivery_line.id, delivered_quantity_after_cancel=1,
            operator_id=operator_id, operation_key='fraction-reverse')
        item.delivered_quantity = 1
        db.flush()
        for lot in lots:
            db.refresh(lot)
        assert sum(lot.quantity_consumed for lot in lots) == ratio
        assert sum(lot.quantity_reserved for lot in lots) == ratio
        assert _delivery_remaining_quantity(db, item) == 1


@pytest.mark.parametrize('ratio,receipts,customer_quantity', [(2, [200], 100), (3, [2, 4], 2)])
def test_order_delivery_print_keeps_customer_quantity_and_projects_physical(routing_app, ratio, receipts, customer_quantity):
    from app.api.deliveries import router, pick_router
    from app.models.order import OrderItem
    routing_app.include_router(router, prefix='/api/deliveries')
    routing_app.include_router(pick_router, prefix='/api/delivery-picks')
    with TestClient(routing_app) as client:
        oid, pid, purchase_line = prepare(routing_app, client, ratio=str(ratio))
        for index, quantity in enumerate(receipts):
            received = receive(client, pid, purchase_line, quantity, f'print-receipt-{index}')
            assert received.status_code == 200, received.text
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            item_id = item.id
            physical_unit = item.external_packaging_purchase_unit_snapshot
        request = {
            'customer_id': routing_app.state.fixture['customer_a'],
                'delivery_date': beijing_today().isoformat(),
            'lines': [{'order_item_id': item_id, 'delivered_quantity': customer_quantity}],
        }
        too_many = client.post('/api/deliveries', json={**request,
            'lines': [{'order_item_id': item_id, 'delivered_quantity': customer_quantity + 1}]})
        assert too_many.status_code == 409, too_many.text
        result = client.post('/api/deliveries', json=request)
        assert result.status_code == 201, result.text
        goods = result.json()['items'][0]['actual_goods_lines'][0]
        assert goods['order_item_id'] == item_id
        assert goods['quantity'] == sum(receipts)
        assert goods['unit'] == physical_unit
        from fractions import Fraction
        sources = result.json()['items'][0]['inventory_sources']
        assert sum(row['quantity_to_pick_stock'] for row in sources) == sum(receipts)
        assert sum(Fraction(row['requirement_numerator'], row['requirement_denominator'])
                   for row in sources) == customer_quantity
        listed = client.get('/api/deliveries?page=1&page_size=10')
        assert listed.status_code == 200, listed.text
        listed_delivery = next(row for row in listed.json()['items'] if row['id'] == result.json()['id'])
        listed_sources = listed_delivery['items'][0]['inventory_sources']
        assert sum(row['quantity_to_pick_stock'] for row in listed_sources) == sum(receipts)
        assert sum(Fraction(row['requirement_numerator'], row['requirement_denominator'])
                   for row in listed_sources) == customer_quantity
        printed = client.get(f"/api/deliveries/{result.json()['id']}/print")
        assert printed.status_code == 200, printed.text
        line = printed.json()['items'][0]
        assert line['quantity'] == customer_quantity
        assert line['actual_goods_quantity'] == sum(receipts)
        picked = client.post(f"/api/deliveries/{result.json()['id']}/pick-task")
        assert picked.status_code == 201, picked.text
        pick_line = picked.json()['items'][0]
        assert pick_line['original_quantity'] == sum(receipts)
        assert sum(row['pick_quantity'] for row in pick_line['location_lines']) == sum(receipts)
        assert {row['unit'] for row in pick_line['location_lines']} == {physical_unit}
        assert all(row['source_type'] == 'finished_inventory' for row in pick_line['location_lines'])
        task_id = picked.json()['id']
        partial = sum(receipts) // 2
        changed = client.put(f"/api/delivery-picks/{task_id}/items/{pick_line['id']}",
                            json={'pick_status': 'partial', 'picked_quantity': partial})
        assert changed.status_code == 200, changed.text
        submitted = client.post(f'/api/delivery-picks/{task_id}/submit')
        assert submitted.status_code == 200, submitted.text
        applied = client.post(f'/api/delivery-picks/{task_id}/apply')
        assert applied.status_code == 200, applied.text
        reprinted = client.get(f"/api/deliveries/{result.json()['id']}/print")
        assert reprinted.status_code == 200, reprinted.text
        assert reprinted.json()['items'][0]['quantity'] == customer_quantity // 2
        assert reprinted.json()['items'][0]['actual_goods_quantity'] == partial
        dispatched = client.put(f"/api/deliveries/{result.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        from app.models.warehouse_inventory import InventoryLot
        with routing_app.state.factory() as db:
            lots = list(db.scalars(select(InventoryLot).where(
                InventoryLot.source_ref_type == 'direct_external_receipt')))
            assert sum(lot.quantity_consumed for lot in lots) == partial
            assert sum(lot.quantity_reserved for lot in lots) == sum(receipts) - partial


def test_surplus_reservation_freezes_physical_quantity_and_replays(routing_app):
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.delivery_quantities import requirement_amount
    from app.services.warehouse_inventory import (
        release_finished_reservation, reserve_finished_surplus_for_delivery,
        WarehouseInventoryError,
    )
    with TestClient(routing_app) as client:
        oid, pid, purchase_line = prepare(routing_app, client, ratio='2')
        response = receive(client, pid, purchase_line, 2000)
        assert response.status_code == 200, response.text
    with routing_app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
        lot = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == 'direct_external_receipt'))
        original = db.scalar(select(InventoryReservation).where(InventoryReservation.inventory_lot_id == lot.id))
        operator_id = original.reserved_by
        # Release real receipt-backed stock through its existing audited service.
        release_finished_reservation(db, reservation_id=original.id, operator_id=operator_id,
            release_reason='测试释放原订单预占', idempotency_key='surplus-test-release', allow_downstream=True)
        reserved = reserve_finished_surplus_for_delivery(db, order_item_id=item.id, quantity=100,
            operator_id=operator_id, operation_key='surplus-test')
        assert sum(row.reserved_stock_quantity for row in reserved) == 200
        assert sum(requirement_amount(row, 'credited_requirement_quantity') for row in reserved) == 100
        db.refresh(lot)
        assert (lot.quantity_available, lot.quantity_reserved) == (1800, 200)
        from app.api.deliveries import _inventory_sources_for_order_item
        sources = _inventory_sources_for_order_item(db, order_item=item,
            planned_delivery_quantity=item.quantity + 100)
        surplus_source = next(row for row in sources if row.get('reservation_id') == reserved[0].id)
        assert surplus_source['quantity_to_pick_stock'] == 200
        assert surplus_source['quantity_to_pick_requirement'] == 100
        replay = reserve_finished_surplus_for_delivery(db, order_item_id=item.id, quantity=100,
            operator_id=operator_id, operation_key='surplus-test')
        assert [row.id for row in replay] == [row.id for row in reserved]
        with pytest.raises(WarehouseInventoryError, match='幂等内容不一致'):
            reserve_finished_surplus_for_delivery(db, order_item_id=item.id, quantity=101,
                operator_id=operator_id, operation_key='surplus-test')
        from datetime import date
        from app.models.delivery import Delivery, DeliveryItem
        from app.services.delivery_snapshots import build_order_delivery_snapshot
        from app.services.warehouse_inventory import (
            consume_finished_reservation, reverse_finished_consumption,
            release_finished_surplus_delivery_reservation,
        )
        delivery = Delivery(delivery_number='SURPLUS-SERVICE-TEST',
            customer_id=lot.finished_detail.owner_customer_id,
            delivery_date=date.today(), status='dispatched')
        db.add(delivery)
        db.flush()
        line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id,
            delivered_quantity=100, **build_order_delivery_snapshot(db, item, 100))
        db.add(line)
        db.flush()
        mutation = consume_finished_reservation(db, reservation_id=reserved[0].id,
            stock_quantity=200, expected_version=lot.version, operator_id=operator_id,
            idempotency_key='surplus-test-consume', delivery_item_id=line.id)
        assert requirement_amount(mutation.allocation, 'credited_requirement_quantity') == 100
        db.refresh(lot)
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (1800, 0, 200)
        reverse_finished_consumption(db, reservation_id=reserved[0].id, stock_quantity=200,
            expected_version=lot.version, operator_id=operator_id,
            idempotency_key='surplus-test-reverse', allocation_id=mutation.allocation.id)
        db.refresh(lot)
        release_finished_surplus_delivery_reservation(db, reservation_id=reserved[0].id,
            stock_quantity=200, expected_version=lot.version, operator_id=operator_id,
            idempotency_key='surplus-test-return')
        db.refresh(lot)
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (2000, 0, 0)


@pytest.mark.parametrize('ratio,stock,customer_quantity', [(1, 200, 100), (2, 200, 100), (2, 199, 99)])
def test_existing_physical_stock_reserves_customer_quantity_for_new_order(routing_app, ratio, stock, customer_quantity):
    from app.api.warehouse import router as warehouse_router
    routing_app.include_router(warehouse_router, prefix='/api/warehouse')
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, InventoryMovement
    from app.services.delivery_quantities import requirement_amount
    from app.services.warehouse_inventory import release_finished_reservation
    with TestClient(routing_app) as client:
        oid, pid, purchase_line = prepare(routing_app, client, ratio=str(ratio))
        received = receive(client, pid, purchase_line, stock)
        assert received.status_code == 200, received.text
        with routing_app.state.factory() as db:
            original_item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            product_id = original_item.product_id
            original = db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == original_item.id))
            lot_id = original.inventory_lot_id
            release_finished_reservation(db, reservation_id=original.id, operator_id=original.reserved_by,
                release_reason='隔离测试释放原订单库存', idempotency_key='adopt-release', allow_downstream=True)
            db.commit()
            lot_version = db.get(InventoryLot, lot_id).version
        preview = client.post('/api/orders/inventory-draft-preview', json={
            'customer_id': routing_app.state.fixture['customer_a'], 'items': [{
                'client_line_id': 'physical-draft', 'product_id': product_id, 'quantity': 200,
                'reservation_plan': {'finished': [{'lot_id': lot_id, 'expected_version': lot_version,
                    'requested_qty': 200, 'confirmed': True}]},
            }]})
        assert preview.status_code == 200, preview.text
        assert preview.json()['items'][0]['finished_planned_quantity'] == stock // ratio
        assert preview.json()['items'][0]['finished_candidate_available_quantity'] == stock // ratio
        payload = _order_payload_for(product_id, routing_app.state.fixture['customer_a'])
        payload['items'][0]['quantity'] = 200
        created = client.post('/api/orders', json=payload)
        assert created.status_code == 201, created.text
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == created.json()['id']))
            item_id = item.id
            lot = db.get(InventoryLot, lot_id)
            version = lot.version
        request = dict(order_item_id=item_id, inventory_lot_id=lot_id, quantity=customer_quantity,
                       expected_version=version, idempotency_key='adopt-existing', warning_acknowledged_codes=[])
        candidates = client.get('/api/warehouse/finished/candidates', params={'order_item_id': item_id})
        assert candidates.status_code == 200, candidates.text
        candidate = next(row for row in candidates.json()['items'] if row['lot_id'] == lot_id)
        assert candidate['quantity_available'] == stock
        assert candidate['customer_quantity_available'] == stock // ratio
        product_candidates = client.get(f'/api/warehouse/finished/products/{product_id}/candidates',
            params={'customer_id': routing_app.state.fixture['customer_a']})
        assert product_candidates.status_code == 200, product_candidates.text
        product_candidate = next(row for row in product_candidates.json()['items'] if row['lot_id'] == lot_id)
        assert product_candidate['quantity_available'] == stock
        assert product_candidate['customer_quantity_available'] == stock // ratio
        assert product_candidate['quantity_contract']['physical_basis'] == ratio
        if ratio == 2:
            insufficient = client.post('/api/warehouse/finished/reservations',
                json={**request, 'quantity': stock // ratio + 1, 'idempotency_key': 'adopt-too-many'})
            assert insufficient.status_code == 409, insufficient.text
        reserved = client.post('/api/warehouse/finished/reservations', json=request)
        assert reserved.status_code == 200, reserved.text
        assert reserved.json()['credited_requirement_quantity'] == customer_quantity
        assert reserved.json()['reserved_stock_quantity'] == customer_quantity * ratio
        assert reserved.json()['credited_requirement_quantity_numerator'] == customer_quantity * ratio
        assert reserved.json()['requirement_quantity_denominator'] == ratio
        replay = client.post('/api/warehouse/finished/reservations', json=request)
        assert replay.status_code == 200, replay.text
        assert client.post('/api/warehouse/finished/reservations', json={**request, 'quantity': customer_quantity - 1}).status_code == 409
        with routing_app.state.factory() as db:
            lot = db.get(InventoryLot, lot_id)
            row = db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == item_id))
            assert row.reserved_stock_quantity == customer_quantity * ratio
            assert requirement_amount(row, 'credited_requirement_quantity') == customer_quantity
            assert (lot.quantity_available, lot.quantity_reserved) == (stock - customer_quantity * ratio, customer_quantity * ratio)
            movement = db.scalar(select(InventoryMovement).where(InventoryMovement.reservation_id == row.id))
            assert movement.quantity == customer_quantity * ratio
            release_finished_reservation(db, reservation_id=row.id, operator_id=row.reserved_by,
                release_reason='隔离测试取消抵扣', idempotency_key='adopt-cancel')
            db.refresh(lot)
            assert (lot.quantity_available, lot.quantity_reserved) == (stock, 0)


@pytest.mark.parametrize('receipts,expected_customer', [([199], 99), ([1, 1], 1), ([1, 2], 1)])
def test_new_order_plan_matches_physical_stock_draft_preview(routing_app, receipts, expected_customer):
    from app.core.time_contract import beijing_today
    from app.api.deliveries import router as delivery_router, pick_router
    routing_app.include_router(delivery_router, prefix='/api/deliveries')
    routing_app.include_router(pick_router, prefix='/api/delivery-picks')
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.delivery_quantities import requirement_amount
    from app.services.warehouse_inventory import release_finished_reservation
    with TestClient(routing_app) as client:
        oid, pid, purchase_line = prepare(routing_app, client, ratio='2')
        for index, quantity in enumerate(receipts):
            assert receive(client, pid, purchase_line, quantity, f'new-plan-receipt-{index}').status_code == 200
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            product_id = item.product_id
            originals = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id)))
            for original in originals:
                release_finished_reservation(db, reservation_id=original.id, operator_id=original.reserved_by,
                    release_reason='隔离测试供新订单采用', idempotency_key=f'new-plan-release-{original.id}', allow_downstream=True)
            db.commit()
            plan = {'finished': [{'lot_id': original.inventory_lot_id,
                'expected_version': db.get(InventoryLot, original.inventory_lot_id).version,
                'requested_qty': 200, 'confirmed': True} for original in originals]}
        draft = {'client_line_id': 'new-physical-plan', 'product_id': product_id,
                 'quantity': 200, 'reservation_plan': plan}
        preview = client.post('/api/orders/inventory-draft-preview', json={
            'customer_id': routing_app.state.fixture['customer_a'],
            'items': [draft, {**draft, 'client_line_id': 'second-physical-plan'}]})
        assert preview.status_code == 200, preview.text
        assert [row['finished_planned_quantity'] for row in preview.json()['items']] == [expected_customer, 0]
        payload = _order_payload_for(product_id, routing_app.state.fixture['customer_a'])
        payload['items'][0].update(quantity=200, reservation_plan=plan, client_line_id='new-physical-plan')
        created = client.post('/api/orders', json=payload)
        assert created.status_code == 201, created.text
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == created.json()['id']))
            reservations = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id)))
            assert item.quantity == 200
            assert sum(requirement_amount(row, 'credited_requirement_quantity') for row in reservations) == expected_customer
            lots = [db.get(InventoryLot, entry['lot_id']) for entry in plan['finished']]
            assert sum(lot.quantity_available for lot in lots) == sum(receipts) - 2 * expected_customer
            assert sum(lot.quantity_reserved for lot in lots) == 2 * expected_customer
            new_item_id = item.id
            from app.services.direct_external_finished import managed
            import json
            assert managed(db, item)
            original_identity = lots[0].finished_detail.physical_basis_json
            identity = json.loads(original_identity)
            identity.pop('quantity_basis')
            lots[0].finished_detail.physical_basis_json = json.dumps(identity)
            assert not managed(db, item)
            lots[0].finished_detail.physical_basis_json = original_identity
            assert managed(db, item)
        delivery = client.post('/api/deliveries', json={
            'customer_id': routing_app.state.fixture['customer_a'], 'delivery_date': beijing_today().isoformat(),
            'lines': [{'order_item_id': new_item_id, 'delivered_quantity': expected_customer}]})
        assert delivery.status_code == 201, delivery.text
        printed = client.get(f"/api/deliveries/{delivery.json()['id']}/print")
        assert printed.status_code == 200, printed.text
        assert printed.json()['items'][0]['quantity'] == expected_customer
        assert printed.json()['items'][0]['actual_goods_quantity'] == 2 * expected_customer
        delivery_id = delivery.json()['id']
        pick = client.post(f'/api/deliveries/{delivery_id}/pick-task')
        assert pick.status_code == 201, pick.text
        pick_item = pick.json()['items'][0]
        assert pick_item['original_quantity'] == 2 * expected_customer
        assert sum(row['pick_quantity'] for row in pick_item['location_lines']) == 2 * expected_customer
        confirmed = client.put(f"/api/delivery-picks/{pick.json()['id']}/items/{pick_item['id']}",
                               json={'pick_status': 'picked'})
        assert confirmed.status_code == 200, confirmed.text
        assert client.post(f"/api/delivery-picks/{pick.json()['id']}/submit").status_code == 200
        dispatched = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        client.put(f'/api/deliveries/{delivery_id}/dispatch')
        reprint = client.get(f'/api/deliveries/{delivery_id}/print')
        assert reprint.status_code == 200, reprint.text
        assert reprint.json()['items'][0]['quantity'] == expected_customer
        with routing_app.state.factory() as db:
            lots = [db.get(InventoryLot, entry['lot_id']) for entry in plan['finished']]
            assert sum(lot.quantity_consumed for lot in lots) == 2 * expected_customer
        cancelled = client.put(f'/api/deliveries/{delivery_id}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        client.put(f'/api/deliveries/{delivery_id}/cancel')
        with routing_app.state.factory() as db:
            lots = [db.get(InventoryLot, entry['lot_id']) for entry in plan['finished']]
            assert sum(lot.quantity_consumed for lot in lots) == 0
            assert sum(lot.quantity_reserved for lot in lots) == 2 * expected_customer
            assert sum(lot.quantity_available for lot in lots) == sum(receipts) - 2 * expected_customer


@pytest.mark.parametrize('ratio', [2, 3])
def test_split_physical_reserved_lot_keeps_fractional_customer_credit(routing_app, ratio):
    from fractions import Fraction
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
    from app.services.delivery_quantities import requirement_amount
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations, active_finished_reserved_qty
    with TestClient(routing_app) as client:
        oid, pid, purchase_line = prepare(routing_app, client, ratio=str(ratio))
        received = receive(client, pid, purchase_line, ratio * 2)
        assert received.status_code == 200, received.text
    with routing_app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
        original = db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id))
        lot = db.get(InventoryLot, original.inventory_lot_id)
        target = db.scalar(select(WarehouseLocation).where(WarehouseLocation.area_code == 'FIN-001',
            WarehouseLocation.source_version == 'TWIN_V1', WarehouseLocation.id != lot.warehouse_location_id)
            .order_by(WarehouseLocation.id))
        assert target is not None
        request = dict(lot_id=lot.id, expected_version=lot.version, quantity=1, location_id=target.id,
                       expected_target_layout_version=target.floor3_layout.version,
                       operator_id=original.reserved_by, idempotency_key='physical-split')
        moved = transfer_finished_lot_between_locations(db, **request)
        child = db.scalar(select(InventoryReservation).where(InventoryReservation.inventory_lot_id == moved.target_lot.id))
        assert child.reserved_stock_quantity == 1
        assert requirement_amount(child, 'credited_requirement_quantity') == Fraction(1, ratio)
        assert requirement_amount(original, 'released_requirement_quantity') == Fraction(1, ratio)
        from app.api.warehouse import _reservation_dict
        projected = _reservation_dict(child, db)
        assert projected['credited_requirement_quantity'] == pytest.approx(1 / ratio)
        assert projected['credited_requirement_quantity_numerator'] == 1
        assert projected['requirement_quantity_denominator'] == ratio
        assert active_finished_reserved_qty(db, item.id) == 2
        assert moved.target_lot.finished_detail.physical_basis_json == lot.finished_detail.physical_basis_json
        repeated = transfer_finished_lot_between_locations(db, **request)
        assert repeated.target_lot.id == moved.target_lot.id
        assert active_finished_reserved_qty(db, item.id) == 2
        from datetime import date
        from app.models.delivery import Delivery, DeliveryItem
        from app.services.delivery_snapshots import build_order_delivery_snapshot
        from app.services.semi_finished_inventory import consume_delivery_item_inventory, reverse_delivery_item_inventory
        delivery = Delivery(delivery_number='SPLIT-QUANTITY', customer_id=lot.finished_detail.owner_customer_id,
                            delivery_date=date.today(), status='dispatched')
        db.add(delivery)
        db.flush()
        delivery_line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id, delivered_quantity=2,
                                    **build_order_delivery_snapshot(db, item, 2))
        db.add(delivery_line)
        db.flush()
        consume_delivery_item_inventory(db, delivery_item_id=delivery_line.id, delivered_quantity_after_dispatch=2,
                                        operator_id=original.reserved_by, operation_key='split-dispatch')
        item.delivered_quantity = 2
        for stock_lot in (lot, moved.target_lot):
            db.refresh(stock_lot)
        assert lot.quantity_consumed + moved.target_lot.quantity_consumed == 2 * ratio
        assert lot.quantity_reserved + moved.target_lot.quantity_reserved == 0
        reverse_delivery_item_inventory(db, delivery_item_id=delivery_line.id, delivered_quantity_after_cancel=0,
                                        operator_id=original.reserved_by, operation_key='split-cancel')
        item.delivered_quantity = 0
        for stock_lot in (lot, moved.target_lot):
            db.refresh(stock_lot)
        assert lot.quantity_consumed + moved.target_lot.quantity_consumed == 0
        assert lot.quantity_reserved + moved.target_lot.quantity_reserved == 2 * ratio
        assert active_finished_reserved_qty(db, item.id) == 2


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


@pytest.mark.parametrize('ratio,received', [('1', 1000), ('2', 2000), ('2', 1)])
def test_receipt_reversal_restores_pending_without_deleting_facts(routing_app, ratio, received):
    from app.models.warehouse_inventory import InventoryLot
    from app.models.external_packaging_purchase import ExternalPackagingReceipt
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client, ratio=ratio)
        first = receive(client, pid, line, received)
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
        assert lot.quantity_consumed == received
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


def test_external_receipt_reverses_split_locations(routing_app):
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    from app.models.external_packaging_purchase import ExternalPackagingReceipt
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client)
        first = receive(client, pid, line, 1000)
        assert first.status_code == 200, first.text
        with routing_app.state.factory() as db:
            lot = db.scalar(select(InventoryLot))
            target = db.scalar(select(WarehouseLocation).where(WarehouseLocation.area_code == 'FIN-001',
                WarehouseLocation.id != lot.warehouse_location_id).order_by(WarehouseLocation.id))
            transfer_finished_lot_between_locations(db, lot_id=lot.id, expected_version=lot.version,
                quantity=400, location_id=target.id, operator_id=1, idempotency_key='external-split-move',
                expected_target_layout_version=target.floor3_layout.version)
            rid = db.scalar(select(ExternalPackagingReceipt.id))
            db.commit()
        payload = dict(idempotency_key='external-split-reverse', reason='隔离分货位撤销', confirmed=True)
        result = client.post(f'/api/external-packaging-receipts/{rid}/reverse', json=payload)
        assert result.status_code == 200, result.text
    with routing_app.state.factory() as db:
        lots = list(db.scalars(select(InventoryLot)))
        assert len(lots) == 2 and sum(l.quantity_available+l.quantity_reserved for l in lots) == 0

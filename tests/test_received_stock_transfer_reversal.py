import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import (_seed_material_and_staging, _create_frozen_sources,
    _freeze_receipt_fact, _receive, _p181_published_map_identity)

@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('tamper', [False, True])
def test_received_finished_stock_reverts_after_location_transfer(requisition_app, split, tamper):
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, InventoryMovement
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client, 'admin')
        source = _create_frozen_sources(client, factory, order_quantity=20, purchase_total=20,
            order_purpose=20, stock_purpose=0)[0]
        frozen = _freeze_receipt_fact(client, source, idempotency_key='transfer-reverse-freeze')
        assert frozen.status_code == 200, frozen.text
        received = _receive(client, source, frozen.json(), quantity=20, idempotency_key='transfer-reverse-receive')
        assert received.status_code == 200, received.text
        with factory() as db:
            completion = db.scalar(select(ProductionCompletion))
            lot = db.get(InventoryLot, completion.inventory_lot_id)
            target = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == 'F1-FIN-001-L002'))
            result = transfer_finished_lot_between_locations(db, lot_id=lot.id, expected_version=lot.version,
                quantity=8 if split else 20, location_id=target.id, operator_id=1,
                idempotency_key='transfer-reverse-move', expected_target_layout_version=2)
            if tamper:
                movement = db.scalar(select(InventoryMovement).where(InventoryMovement.movement_type == 'location_transfer').order_by(InventoryMovement.id))
                movement.quantity += 1
            db.commit()
            before = [(r.id, r.quantity_available, r.quantity_reserved, r.version) for r in db.scalars(select(InventoryLot).order_by(InventoryLot.id))]
        url = f"/api/incoming/receipt-items/{received.json()['receipt_item_id']}/revert"
        payload = {'reason':'撤销分货位试验收料','idempotency_key':'transfer-reverse-final'}
        result = client.put(url, json=payload)
        if tamper:
            assert result.status_code == 409, result.text
            with factory() as db:
                assert [(r.id, r.quantity_available, r.quantity_reserved, r.version) for r in db.scalars(select(InventoryLot).order_by(InventoryLot.id))] == before
            return
        assert result.status_code == 200, result.text
        with factory() as db:
            lots = list(db.scalars(select(InventoryLot).where(InventoryLot.inventory_type == 'finished')))
            assert sum(r.quantity_available + r.quantity_reserved for r in lots) == 0
            assert len(lots) == 2
            count = len(list(db.scalars(select(InventoryMovement))))
        assert client.put(url, json=payload).status_code == 200
        with factory() as db:
            assert len(list(db.scalars(select(InventoryMovement)))) == count


def test_order_cancel_only_releases_preexisting_stock(requisition_app):
    from datetime import date
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    from app.services.warehouse_inventory import manual_finished_in, reserve_finished_inventory
    from app.models.order import OrderItem
    from app.api.admin_order_reversal import router
    app, factory = requisition_app
    app.include_router(router, prefix='/api/orders')
    _seed_material_and_staging(factory)
    with factory() as db:
        item = db.get(OrderItem, 1)
        from app.models.product import Product
        from app.models.material import Material
        product = db.get(Product, item.product_id)
        product.report_length_mm, product.report_width_mm = 1000, 600
        product.box_style = 'A1'
        db.get(Material, product.material_id).price_unit = 'm2'
        db.flush()
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == 'F1-FIN-001-L002'))
        lot = manual_finished_in(db, customer_id=item.order.customer_id, product_id=item.product_id,
            location_id=location.id, quantity=200, stock_date=date(2026,9,1), source_type='purchase_reserve',
            remarks='原有库存试验', operator_id=1, idempotency_key='original-stock-in', expected_layout_version=2)
        stock = lot.lot if hasattr(lot, 'lot') else lot
        reservation = reserve_finished_inventory(db, order_item_id=item.id, inventory_lot_id=stock.id,
            quantity=100, expected_version=stock.version, operator_id=1,
            idempotency_key='original-stock-reserve', warning_acknowledged_codes=[])
        lot_id = stock.id
        db.commit()
    with TestClient(app) as client:
        _login(client, 'admin')
        body = dict(order_ids=[1], mode='delete_order')
        plan = client.post('/api/orders/admin-disposition/preview', json=body)
        assert plan.status_code == 200, plan.text
        assert plan.json()['inventory'][0]['action'] == '保留实物、解除本单绑定'
        result = client.post('/api/orders/admin-disposition/execute', json={**body,
            'reviewed_hash':plan.json()['reviewed_hash'], 'operation_key':'original-stock-cancel'})
        assert result.status_code == 200, result.text
    with factory() as db:
        stock = db.get(InventoryLot, lot_id)
        assert stock.quantity_available == 200 and stock.quantity_reserved == 0


@pytest.mark.parametrize("case", ["normal", "quantity_changed", "audit_failure"])
def test_receipt_reversal_after_whole_pallet_move(requisition_app, monkeypatch, case):
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, InventoryMovement
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.floor3_locations import move_pallet
    from app.services.admin_order_reversal_scope import may_reverse_at_current_location
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, 'admin')
        source = _create_frozen_sources(client, factory, order_quantity=20, purchase_total=20,
            order_purpose=20, stock_purpose=0)[0]
        frozen = _freeze_receipt_fact(client, source, idempotency_key='pallet-freeze')
        assert frozen.status_code == 200, frozen.text
        received = _receive(client, source, frozen.json(), quantity=20, idempotency_key='pallet-receive')
        assert received.status_code == 200, received.text
        receipt_id = received.json()['receipt_item_id']
        with factory() as db:
            completion = db.scalar(select(ProductionCompletion))
            lot = db.get(InventoryLot, completion.inventory_lot_id)
            lot_id, item_id = lot.id, completion.order_item_id
            pallet = lot.pallet_item.pallet
            target = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == 'F1-FIN-001-L002'))
            move_pallet(db, pallet_id=pallet.id, expected_version=pallet.version,
                to_location_id=target.id, remarks='整栈板移动', operator_id=1,
                idempotency_key='pallet-move', require_published_target=True,
                expected_target_layout_version=2)
            if case == 'quantity_changed':
                lot.quantity_reserved -= 1
            db.commit()
            before = (lot.quantity_available, lot.quantity_reserved, lot.version)
            movement_count = len(list(db.scalars(select(InventoryMovement))))
        if case == 'audit_failure':
            import app.api.incoming as incoming
            def fail(*args, **kwargs):
                raise RuntimeError('injected reversal audit failure')
            monkeypatch.setattr(incoming, '_record_reversal_fact', fail)
        url = f'/api/incoming/receipt-items/{receipt_id}/revert'
        payload = {'reason':'撤销移库收料', 'idempotency_key':'pallet-revert'}
        result = client.put(url, json=payload)
        assert not may_reverse_at_current_location(item_id)
        if case != 'normal':
            assert result.status_code == (409 if case == 'quantity_changed' else 500), result.text
            with factory() as db:
                lot = db.get(InventoryLot, lot_id)
                assert (lot.quantity_available, lot.quantity_reserved, lot.version) == before
                assert db.get(IncomingReceiptItem, receipt_id).status == 'posted'
                assert len(list(db.scalars(select(InventoryMovement)))) == movement_count
            return
        assert result.status_code == 200, result.text
        with factory() as db:
            lot = db.get(InventoryLot, lot_id)
            assert lot.quantity_available + lot.quantity_reserved == 0
            assert db.get(IncomingReceiptItem, receipt_id).status == 'reversed'
            movement_count = len(list(db.scalars(select(InventoryMovement))))
        assert client.put(url, json=payload).status_code == 200
        assert client.put(url, json={**payload, 'reason':'异载荷'}).status_code == 409
        with factory() as db:
            assert len(list(db.scalars(select(InventoryMovement)))) == movement_count

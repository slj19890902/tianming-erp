"""Employee conversion preserves physical and financial inventory facts."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_stock_processing_auto import completion, receive, row, stock_replenishment_app
from app.api.stock_preparation import router
from app.models.stock_preparation import StockPreparationCommand
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer, InventoryMovement
from app.models.warehouse_goods import WarehouseGoodsProfile


def prepared(client):
    receive(client)
    initial = row(client)
    posted = client.post(f"/api/production/stock-preparation/{initial['receipt_item_id']}/actions",
                         json=completion(initial))
    assert posted.status_code == 200, posted.text
    current = row(client)
    job = next(j for j in current['jobs'] if j['status'] == 'completed')
    body = dict(operation_key='employee-finish-0001', job_id=job['id'], job_version=job['version'],
                output_lot_id=job['output_lot_id'], output_version=job['output_version'],
                quantity=3, location_id=7, layout_version=1, confirm_finished=True)
    return current['receipt_item_id'], job, body


def test_employee_partial_conversion_and_replay(stock_replenishment_app):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        receipt_id, job, body = prepared(client)
        url = f'/api/production/stock-preparation/{receipt_id}/finish-output'
        with factory() as db:
            actor = db.scalar(select(User))
            actor.role = 'workshop'
            db.commit()
        response = client.post(url, json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert client.post(url, json=body).json() == result
        assert client.post(url + '-result', json={'original_request': body}).json()['result'] == result
        assert client.post(url, json=dict(body, quantity=4)).status_code == 409
        assert client.post(url, json=dict(body, operation_key='employee-finish-0002')).status_code == 409
        with factory() as db:
            source = db.get(InventoryLot, job['output_lot_id'])
            finished = db.get(InventoryLot, result['output_lot_id'])
            assert (source.quantity_available, source.quantity_consumed) == (5, 3)
            assert finished.quantity_available == 3 and finished.inventory_type == 'finished'
            assert finished.finished_detail.product_id == result['product_id']
            from app.services.inventory_valuation import frozen_cost
            assert frozen_cost(finished, db)[0] == frozen_cost(source, db)[0]
            assert db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == result['movement_key'])).quantity == 3


def test_identity_scope_and_location_fail_closed(stock_replenishment_app):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        receipt_id, job, body = prepared(client)
        url = f'/api/production/stock-preparation/{receipt_id}/finish-output'
        assert client.post(url, json=dict(body, location_id=999999)).status_code in {404, 409}
        assert client.post(url, json=dict(body, layout_version=999)).status_code == 409
        with factory() as db:
            actor = db.scalar(select(User))
            actor.role = 'workshop'
            actor.customer_access_mode = 'selected'
            db.commit()
        assert client.post(url, json=body).status_code == 403
        with factory() as db:
            actor = db.scalar(select(User))
            actor.customer_access_mode = 'all'
            profile = db.get(WarehouseGoodsProfile, job['output_lot_id'])
            import json
            facts = json.loads(profile.data_json)
            facts['remaining_processes'] = ['模切']
            profile.data_json = json.dumps(facts)
            db.commit()
        assert client.post(url, json=body).status_code == 409
        with factory() as db:
            source = db.get(InventoryLot, job['output_lot_id'])
            assert (source.quantity_available, source.quantity_consumed) == (8, 0)
            assert db.get(StockPreparationCommand, body['operation_key']) is None


def test_audit_failure_rolls_back_conversion(stock_replenishment_app, monkeypatch):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app, raise_server_exceptions=False) as client:
        receipt_id, job, body = prepared(client)
        import app.services.stock_preparation_finish as service
        def fail(*args, **kwargs):
            raise RuntimeError('audit unavailable')
        monkeypatch.setattr(service, 'append_audit_event', fail)
        url = f'/api/production/stock-preparation/{receipt_id}/finish-output'
        assert client.post(url, json=body).status_code == 500
        with factory() as db:
            source = db.get(InventoryLot, job['output_lot_id'])
            assert (source.quantity_available, source.quantity_consumed) == (8, 0)
            assert db.get(StockPreparationCommand, body['operation_key']) is None


def test_identity_cost_and_permission_fail_closed(stock_replenishment_app):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        receipt_id, job, body = prepared(client)
        url = f'/api/production/stock-preparation/{receipt_id}/finish-output'
        assert client.post(url, json=dict(body, confirm_finished=False)).status_code == 409
        with factory() as db:
            source = db.get(InventoryLot, job['output_lot_id'])
            source.estimated_unit_cost_snapshot = None
            db.commit()
        failure = client.post(url, json=body)
        assert failure.status_code == 409 and '成本' in failure.text
        with factory() as db:
            source = db.get(InventoryLot, job['output_lot_id'])
            assert source.quantity_available == 8 and source.quantity_consumed == 0
            assert db.get(StockPreparationCommand, body['operation_key']) is None
            actor = db.scalar(select(User))
            actor.role = 'sales'
            db.commit()
        assert client.post(url, json=body).status_code == 403


def test_converted_finished_stock_can_move_partial_and_again(stock_replenishment_app):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        receipt_id, _, body = prepared(client)
        response = client.post(f'/api/production/stock-preparation/{receipt_id}/finish-output', json=body)
        assert response.status_code == 200, response.text
        output_id = response.json()['output_lot_id']
    from app.services.inventory_valuation import frozen_cost
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations
    with factory() as db:
        output = db.get(InventoryLot, output_id)
        original_cost = frozen_cost(output, db)[0]
        first = transfer_finished_lot_between_locations(db, lot_id=output.id,
            expected_version=output.version, quantity=2, location_id=8,
            operator_id=1, idempotency_key='finish-move-partial', expected_target_layout_version=1)
        db.commit()
        child = db.get(InventoryLot, first.target_lot.id)
        assert child.id != output_id and frozen_cost(child, db)[0] == original_cost
        assert (db.get(InventoryLot, output_id).quantity_available, child.quantity_available) == (1, 2)
        repeated = transfer_finished_lot_between_locations(db, lot_id=output_id,
            expected_version=1, quantity=2, location_id=8, operator_id=1,
            idempotency_key='finish-move-partial', expected_target_layout_version=1)
        assert repeated.target_lot.id == child.id
        second = transfer_finished_lot_between_locations(db, lot_id=child.id,
            expected_version=child.version, quantity=1, location_id=7,
            operator_id=1, idempotency_key='finish-move-partial-again', expected_target_layout_version=1)
        db.commit()
        grandchild = db.get(InventoryLot, second.target_lot.id)
        assert grandchild.id not in {output_id, child.id}
        assert frozen_cost(grandchild, db)[0] == original_cost
        assert sum(db.get(InventoryLot, lid).quantity_available for lid in (output_id, child.id, grandchild.id)) == 3
        assert len(list(db.scalars(select(InventoryLotTransfer).where(
            InventoryLotTransfer.target_lot_id == grandchild.id)))) == 1
        edge = db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id == grandchild.id))
        edge.quantity += 1
        edge.available_quantity += 1
        db.flush()
        assert frozen_cost(grandchild, db)[0] is None
        db.rollback()

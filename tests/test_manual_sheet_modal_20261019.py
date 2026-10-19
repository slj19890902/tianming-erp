import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_manual_sheet_deduction_20261013 import seed, ordinary_seed, BASE, payload


def seed_pair(sessions):
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    first = seed(sessions)
    with sessions() as db:
        db.get(InventoryLot, first).lot_number = 'MODAL-FIRST'
        db.get(WarehouseLocation, db.get(InventoryLot, first).warehouse_location_id).location_code = 'MODAL-FIRST'
        db.commit()
    return first, seed(sessions)


def batch_request(data, quantity=3):
    return {'component_type':'whole', 'match_mode':data['match_mode'],
        'expected_order_item_revision':data['expected_order_item_revision'],
        'idempotency_key':'manual-sheet-modal-batch-one', 'items':[
            {key:value for key,value in payload({**data,'items':[row]}, quantity).items()
                if key not in ('component_type','expected_order_item_revision','idempotency_key')}
            for row in data['items']]}


def change_stock(sessions, lot_id, **values):
    from app.models.warehouse_inventory import SemiFinishedInventoryDetail
    with sessions() as db:
        row = db.get(SemiFinishedInventoryDetail, lot_id)
        for key, value in values.items():
            setattr(row, key, value)
        db.commit()


@pytest.mark.parametrize('dimensions', [(700, 1000), (1000, 724), (2000, 1400)])
def test_default_only_exact_direction(composite_requisition_app, dimensions):
    app, sessions = composite_requisition_app
    lot_id = seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=dimensions[0], board_width_mm=dimensions[1])
    with TestClient(app) as client:
        _login(client)
        assert client.get(BASE+'/manual-inventory').json()['items'] == []


@pytest.mark.parametrize('flute', ['A', 'B', 'BE'])
def test_flute_is_hard_gate_even_in_swapped_mode(composite_requisition_app, flute):
    app, sessions = composite_requisition_app
    lot_id = seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=700, board_width_mm=1000, flute_type=flute,
                 layer_count=3 if flute in ('A', 'B') else 5)
    with TestClient(app) as client:
        _login(client)
        assert client.get(BASE+'/manual-inventory?match_mode=swapped').json()['items'] == []


def test_swap_opt_in_is_frozen_and_default_payload_cannot_use_it(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryReservation
    app, sessions = composite_requisition_app
    lot_id = seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=700, board_width_mm=1000)
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE+'/manual-inventory?match_mode=swapped').json()
        assert data['match_mode'] == 'swapped'
        assert len(data['items']) == 1
        assert data['items'][0]['cut_plan']['rotated'] is True
        request = payload(data, 3)
        assert client.post(BASE+'/manual-reserve', json=request).status_code == 409
        request['match_mode'] = 'swapped'
        response = client.post(BASE+'/manual-reserve', json=request)
        assert response.status_code == 200, response.text
        assert client.post(BASE+'/manual-reserve', json=request).json()['replayed'] is True
    with sessions() as db:
        reservation = db.scalar(select(InventoryReservation))
        plan = json.loads(reservation.cut_plan_json)
        assert plan['rotated'] is True
        assert (plan['target_length_mm'], plan['target_width_mm']) == (1000, 700)


def test_multi_lot_submit_is_atomic_and_replayable(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    app, sessions = composite_requisition_app
    first, second = seed_pair(sessions)
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE+'/manual-inventory').json()
        request = batch_request(data)
        stale = json.loads(json.dumps(request))
        stale['items'][-1]['expected_version'] += 1
        assert client.post(BASE+'/manual-reserve-batch', json=stale).status_code == 409
        with sessions() as db:
            assert db.scalar(select(func.count(InventoryReservation.id))) == 0
            assert db.get(InventoryLot, first).quantity_available == 204
        response = client.post(BASE+'/manual-reserve-batch', json=request)
        assert response.status_code == 200, response.text
        assert response.json()['stock_quantity'] == 6
        assert response.json()['remaining_required_piece_qty'] == 14
        assert client.post(BASE+'/manual-reserve-batch', json=request).json()['replayed'] is True
        request['items'][0]['stock_quantity'] = 4
        assert client.post(BASE+'/manual-reserve-batch', json=request).status_code == 409
    with sessions() as db:
        assert db.scalar(select(func.count(InventoryReservation.id))) == 2
        assert [db.get(InventoryLot, n).quantity_available for n in (first, second)] == [201, 201]


def test_batch_failure_after_first_write_rolls_back_all(composite_requisition_app, monkeypatch):
    import app.api.warehouse as api
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement
    app, sessions = composite_requisition_app
    first, second = seed_pair(sessions)
    original = api.append_audit_event
    writes = []
    def fault(*args, **kwargs):
        if kwargs.get('action_code') == 'bom_manual_sheet_reserve':
            writes.append(1)
            if len(writes) == 2:
                raise RuntimeError('second allocation audit failed')
        return original(*args, **kwargs)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        data = client.get(BASE+'/manual-inventory').json()
        monkeypatch.setattr(api, 'append_audit_event', fault)
        assert client.post(BASE+'/manual-reserve-batch', json=batch_request(data)).status_code == 500
        assert len(writes) == 2
    with sessions() as db:
        assert db.scalar(select(func.count(InventoryReservation.id))) == 0
        assert db.scalar(select(func.count(OrderItemSemiRequirement.id))) == 0
        assert [db.get(InventoryLot, n).quantity_available for n in (first, second)] == [204, 204]


def test_exact_stock_prevents_automatic_or_forged_swap_mode(composite_requisition_app):
    app, sessions = composite_requisition_app
    first, _ = seed_pair(sessions)
    change_stock(sessions, first, board_length_mm=700, board_width_mm=1000)
    with TestClient(app) as client:
        _login(client)
        exact = client.get(BASE+'/manual-inventory').json()
        assert len(exact['items']) == 1
        assert exact['can_further_match'] is False
        assert client.get(BASE+'/manual-inventory?match_mode=swapped').json()['items'] == []


@pytest.mark.parametrize('flute', ['B', 'A', 'AB'])
def test_b_flute_box_only_accepts_b_stock(composite_requisition_app, flute):
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.product import Product
    app, sessions = composite_requisition_app
    lot_id = seed(sessions)
    with sessions() as db:
        snapshot = db.get(SalesOrderItemBomComponent, 1)
        snapshot.snapshot_component_flute_type = 'B'
        product = db.get(Product, snapshot.component_product_id)
        product.flute_type = 'B'
        product.layer_count = 3
        db.commit()
    change_stock(sessions, lot_id, flute_type=flute, layer_count=5 if flute=='AB' else 3)
    with TestClient(app) as client:
        _login(client)
        assert bool(client.get(BASE+'/manual-inventory').json()['items']) is (flute=='B')


def test_ordinary_swapped_batch_consumes_under_saved_plan(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.semi_finished_inventory import consume_semi_finished_reservation
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=700, board_width_mm=1000)
    base='/api/warehouse/semi-finished/order-items/1'
    with TestClient(app) as client:
        _login(client)
        data=client.get(base+'/manual-inventory?match_mode=swapped').json()
        result=client.post(base+'/manual-reserve-batch', json=batch_request(data))
        assert result.status_code==200, result.text
        rid=result.json()['reservation_ids'][0]
    with sessions() as db:
        consume_semi_finished_reservation(db, reservation_id=rid, stock_quantity=3,
            expected_version=db.get(InventoryLot,lot_id).version, operator_id=1,
            idempotency_key='ordinary-swapped-production')
        db.commit()
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(201,0,3)
        assert json.loads(db.get(InventoryReservation,rid).cut_plan_json)['rotated'] is True


@pytest.mark.parametrize('owned', [False, True])
def test_swapped_stock_preserves_mold_yield_once(composite_requisition_app, owned):
    app, sessions = composite_requisition_app
    lot_id = seed(sessions, yield_factor=3, owned=owned)
    change_stock(sessions, lot_id, board_length_mm=700, board_width_mm=1000)
    with TestClient(app) as client:
        _login(client)
        data=client.get(BASE+'/manual-inventory?match_mode=swapped').json()
        assert data['items'][0]['stock_yield_per_sheet']==3
        response=client.post(BASE+'/manual-reserve-batch',json=batch_request(data,2))
        assert response.status_code==200,response.text
        assert response.json()['allocated_requirement_quantity']==6
        assert response.json()['remaining_required_piece_qty']==14

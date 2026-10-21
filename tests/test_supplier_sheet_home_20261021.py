import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_manual_sheet_deduction_20261013 import seed, BASE, payload
from tests.test_manual_sheet_modal_20261019 import change_stock, batch_request


@pytest.mark.parametrize('size,output,tier', [((2000, 1400), 4, 'cut'), ((800, 2100), 2, 'swapped')])
def test_auto_discovery_reserve_and_actual_consumption(composite_requisition_app, size, output, tier):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.semi_finished_inventory import consume_semi_finished_reservation
    app, sessions = composite_requisition_app
    lot_id = seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=size[0], board_width_mm=size[1])
    with TestClient(app) as client:
        _login(client)
        r = client.get(BASE+'/manual-inventory?match_mode=auto')
        assert r.status_code == 200, r.text
        data = r.json()
        row = data['items'][0]
        assert row['recommendation_kind'] == tier
        assert row['stock_yield_per_sheet'] == output
        request = batch_request(data, 2)
        saved = client.post(BASE+'/manual-reserve-batch', json=request)
        assert saved.status_code == 200, saved.text
        assert saved.json()['allocated_requirement_quantity'] == 2 * output
        assert client.post(BASE+'/manual-reserve-batch', json=request).json()['replayed'] is True
        rid = saved.json()['reservation_ids'][0]
    with sessions() as db:
        reservation = db.get(InventoryReservation, rid)
        assert json.loads(reservation.cut_plan_json)['rotated'] is (tier == 'swapped')
        consume_semi_finished_reservation(db, reservation_id=rid, stock_quantity=2,
            expected_version=db.get(InventoryLot, lot_id).version, operator_id=1,
            idempotency_key='auto-sheet-production')
        db.commit()
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (202, 0, 2)


def test_auto_orders_exact_then_large_then_swapped(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    app, sessions = composite_requisition_app
    ids = []
    for i, size in enumerate([(800, 2100), (2000, 1400), (1000, 700)]):
        lot_id = seed(sessions)
        change_stock(sessions, lot_id, board_length_mm=size[0], board_width_mm=size[1])
        with sessions() as db:
            lot = db.get(InventoryLot, lot_id)
            lot.lot_number = f'AUTO-{i}'
            db.get(WarehouseLocation, lot.warehouse_location_id).location_code = f'AUTO-{i}'
            db.commit()
        ids.append(lot_id)
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE+'/manual-inventory?match_mode=auto').json()
        assert [r['lot_id'] for r in data['items']] == list(reversed(ids))
        assert [r['recommendation_kind'] for r in data['items']] == ['exact', 'cut', 'swapped']


@pytest.mark.parametrize('flute', ['A', 'B', 'BE'])
def test_auto_large_still_rejects_wrong_flute(composite_requisition_app, flute):
    app, sessions = composite_requisition_app
    lot_id = seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=2000, board_width_mm=1400,
        flute_type=flute, layer_count=3 if flute in ('A', 'B') else 5)
    with TestClient(app) as client:
        _login(client)
        r = client.get(BASE+'/manual-inventory?match_mode=auto')
        assert r.status_code == 200, r.text
        assert r.json()['items'] == []


def test_auto_preview_cannot_be_replayed_as_exact(composite_requisition_app):
    app, sessions = composite_requisition_app
    lot_id = seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=2000, board_width_mm=1400)
    with TestClient(app) as client:
        _login(client)
        r = client.get(BASE+'/manual-inventory?match_mode=auto')
        assert r.status_code == 200, r.text
        assert client.post(BASE+'/manual-reserve', json=payload(r.json(), 2)).status_code == 409


def test_mixed_cut_and_rotated_batch_is_atomic_and_does_not_overreserve(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from tests.test_manual_sheet_modal_20261019 import seed_pair
    app, sessions = composite_requisition_app
    first, second = seed_pair(sessions)
    change_stock(sessions,first,board_length_mm=2000,board_width_mm=1400)
    change_stock(sessions,second,board_length_mm=800,board_width_mm=2100)
    with TestClient(app) as client:
        _login(client)
        data=client.get(BASE+'/manual-inventory?match_mode=auto').json()
        request=batch_request(data,2)
        request['items'][0]['stock_quantity']=4  # 16 + 4 = 20 frozen pieces
        bad=json.loads(json.dumps(request));bad['items'][1]['expected_version']+=1
        assert client.post(BASE+'/manual-reserve-batch',json=bad).status_code==409
        with sessions() as db:
            assert db.get(InventoryLot,first).quantity_available==204
            assert db.get(InventoryLot,second).quantity_available==204
            assert not list(db.scalars(select(InventoryReservation)))
        saved=client.post(BASE+'/manual-reserve-batch',json=request)
        assert saved.status_code==200,saved.text
        assert saved.json()['allocated_requirement_quantity']==20
        assert saved.json()['remaining_required_piece_qty']==0

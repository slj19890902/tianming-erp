from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from tests.test_mobile_dimension_stock import stock_api, stocktake_api, BASE, _login


def test_near_includes_both_sides_sorted_before_paging_and_scoped(stock_api):
    app, factory, ids = stock_api
    with factory() as db:
        db.get(InventoryLot, ids['lot1']).finished_detail.length_mm = 490
        db.get(InventoryLot, ids['lot2']).finished_detail.length_mm = 500
        db.get(InventoryLot, ids['other_lot']).finished_detail.length_mm = 503
        db.commit()
        before = db.scalar(select(func.count(InventoryMovement.id)))
    query = BASE + '?kind=box&length=500&length_op=near&width=300&width_op=near&height=200&height_op=near'
    with TestClient(app) as client:
        assert client.get(query).status_code == 401
        _login(client, 'n035-admin')
        data = client.get(query+'&limit=2').json()
        assert data['total'] == 3
        assert [v['id'] for v in data['items']] == [ids['lot2'], ids['other_lot']]
        assert client.get(query+'&limit=2&offset=2').json()['items'][0]['id'] == ids['lot1']
        assert 'placement_status' in data['items'][0] and 'area_code' in data['items'][0]
        assert client.get(query+'&flute=DOESNOTEXIST').json()['total'] == 0
        # Existing mobile defaults still require >=, desktop near is explicit.
        assert client.get(BASE+'?kind=box&length=500').json()['total'] == 2
        assert client.get(BASE+'?kind=box&length=500&length_op=le').json()['total'] == 2
        _login(client, 'n035-restricted')
        assert client.get(query).json()['total'] == 0
    with factory() as db:
        assert db.scalar(select(func.count(InventoryMovement.id))) == before


def test_board_near_keeps_axis_order_and_missing_dimension_not_exact(stock_api):
    app, factory, ids = stock_api
    with TestClient(app) as client:
        _login(client, 'n035-admin')
        data = client.get(BASE+'?kind=board&length=910&width=590&length_op=near&width_op=near&flute=none').json()
        assert data['items'][0]['id'] == ids['semi_lot']
        assert data['items'][0]['dimensions'] == [900,600]
        assert client.get(BASE+'?kind=board&length=910&width=590&length_op=ge&width_op=near').json()['total'] == 0
        assert client.get(BASE+'?kind=box&length=0&length_op=near').status_code == 422

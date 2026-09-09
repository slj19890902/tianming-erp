import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.test_p1_47d_inventory_adjustment import stocktake_app
from app.models.customer import Customer
from app.models.warehouse_inventory import InventoryLot
from app.services.rack_information_labels import rack_information_contents


def test_information_label_api_uses_current_origin_and_does_not_require_binding(stocktake_app):
    from fastapi.testclient import TestClient
    from tests.test_p1_47d_inventory_adjustment import _login
    app, factory, ids, _ = stocktake_app
    with factory() as db:
        db.get(Customer, ids['customer']).chinese_short_name = '甲客户'
        db.commit()
    with TestClient(app) as client:
        _login(client, 'p147d-admin')
        response = client.get(f"/api/warehouse/locations/{ids['loc_fg1']}/label?content=shelf-information")
        assert response.status_code == 200, response.text
        label = response.json()
        assert label['shelf_contents'][0]['product_id'] == ids['product']
        assert '/sp/' not in label['lookup_url']
        assert f"location_id={ids['loc_fg1']}" in label['lookup_url']


def test_actual_inventory_without_binding_preserves_scope_and_snapshots(stocktake_app):
    _, factory, ids, _ = stocktake_app
    with factory() as db:
        db.get(Customer, ids['customer']).chinese_short_name = '甲客户'
        seen = []
        before = [(x.id, x.quantity_available, x.quantity_reserved) for x in db.scalars(select(InventoryLot))]
        rows = rack_information_contents(db, ids['loc_fg1'], seen.append)
        assert rows and rows[0]['customer_short_name'] == '甲客户'
        assert rows[0]['product_id'] == ids['product']
        assert all('quantity' not in key for row in rows for key in row)
        assert seen and set(seen) == {ids['customer']}
        assert before == [(x.id, x.quantity_available, x.quantity_reserved) for x in db.scalars(select(InventoryLot))]
        assert rack_information_contents(db, ids['loc_fg1_add'], seen.append) == []
        def forbidden(_):
            raise HTTPException(403, '客户受限')
        with pytest.raises(HTTPException) as error:
            rack_information_contents(db, ids['loc_fg1'], forbidden)
        assert error.value.status_code == 403
        db.get(Customer, ids['customer']).chinese_short_name = None
        with pytest.raises(HTTPException) as error:
            rack_information_contents(db, ids['loc_fg1'], seen.append)
        assert error.value.status_code == 409

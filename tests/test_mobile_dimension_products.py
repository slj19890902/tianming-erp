from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.customer import Customer
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from tests.test_mobile_dimension_stock import stock_api, stocktake_api, _login, BASE


QUERY = dict(kind='box', length='380', width='340', height='420',
             length_op='near', width_op='near', height_op='near', include_products=True)


def seed(factory, ids):
    with factory() as db:
        customer_id = db.get(InventoryLot, ids['lot1']).finished_detail.owner_customer_id
        def add(code, dimensions, **extra):
            p = Product(customer_id=customer_id, product_code=code, customer_material_code=code,
                        product_name='空白纸箱', box_category='normal', flute_type='B',
                        length_mm=dimensions[0], width_mm=dimensions[1], height_mm=dimensions[2], **extra)
            db.add(p)
            return p
        # Insert less close rows first: ranking must precede the page limit.
        add('NEAR', (385, 337, 420))
        add('EDGE', (370, 350, 430))
        exact = add('000380', (380, 340, 420))
        add('FAR', (391, 340, 420))
        add('ROTATED', (340, 380, 420))
        add('FLAT', (380, 340, None))
        add('INACTIVE', (380, 340, 420), is_active=False)
        add('DELETED', (380, 340, 420), deleted_at=datetime(2026, 1, 1))
        inactive = Customer(name='停用客户', is_active=False)
        db.add(inactive); db.flush()
        hidden = add('INACTIVE-CUSTOMER', (380, 340, 420))
        hidden.customer_id = inactive.id
        db.commit()
        return exact.id


def test_unstocked_products_rank_paginate_and_do_not_write(stock_api):
    app, factory, ids = stock_api
    exact_id = seed(factory, ids)
    with factory() as db:
        before = [(lot.id, lot.version, lot.quantity_available) for lot in db.scalars(select(InventoryLot))]
    with TestClient(app) as client:
        assert client.get(BASE, params=QUERY).status_code == 401
        _login(client, 'n035-workshop')
        response = client.get(BASE, params={**QUERY, 'product_limit': 1})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['total'] == 0
        assert data['product_total'] == 3
        match = data['product_matches'][0]
        assert match['product_id'] == exact_id
        assert match['code'] == '000380' and match['customer'] == 'N035 测试客户'
        assert match['dimensions'] == [380, 340, 420]
        assert match['dimension_deltas'] == [0, 0, 0] and match['match_exact'] is True
        assert match['has_stock'] is False and match['can_take'] is False
        assert 'id' not in match and 'location_id' not in match
        assert 'no-store' in response.headers['cache-control']
        rest = client.get(BASE, params={**QUERY, 'product_offset': 1}).json()
        assert [p['code'] for p in rest['product_matches']] == ['NEAR', 'EDGE']
        assert rest['product_matches'][0]['dimension_deltas'] == [5, -3, 0]
        assert client.get(BASE, params={**QUERY, 'near_mm': 20}).json()['product_total'] == 4
        assert client.get(BASE, params={**QUERY, 'length_op': 'eq'}).json()['product_total'] == 1
        assert client.get(BASE, params={**QUERY, 'flute': 'E'}).json()['product_total'] == 0
        _login(client, 'n035-restricted')
        hidden = client.get(BASE, params=QUERY).json()
        assert hidden['product_total'] == 0 and hidden['product_matches'] == []
    with factory() as db:
        assert before == [(lot.id, lot.version, lot.quantity_available) for lot in db.scalars(select(InventoryLot))]
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0


def test_candidates_require_all_box_dimensions_and_stock_is_real(stock_api):
    app, factory, ids = stock_api
    seed(factory, ids)
    with factory() as db:
        lot = db.get(InventoryLot, ids['lot1'])
        p = db.get(Product, lot.finished_detail.product_id)
        p.length_mm, p.width_mm, p.height_mm = 380, 340, 420
        p.customer_material_code = 'STOCKED'
        db.commit()
    with TestClient(app) as client:
        _login(client, 'n035-admin')
        data = client.get(BASE, params=QUERY).json()
        assert next(p for p in data['product_matches'] if p['code'] == 'STOCKED')['has_stock'] is True
        assert data['total'] == 0  # Current lot snapshots remain 500×300×200.
        partial = {k: v for k, v in QUERY.items() if k != 'height'}
        assert client.get(BASE, params=partial).json()['product_matches'] == []
        assert client.get(BASE, params={'kind': 'board', 'length': '380', 'include_products': True}).json()['product_matches'] == []
        assert client.get(BASE, params={**QUERY, 'near_mm': 51}).status_code == 422


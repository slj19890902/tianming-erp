from decimal import Decimal
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from test_floor3_locations_api import floor3_app, _login
from app.models.product import Product
from app.models.customer import Customer
from app.services.stocktake_spec_search import parse_dimensions, dimension_score


@pytest.mark.parametrize('query',['800×600×200','800*600*200','800 x 600 x 200 mm','800＊600＊200毫米'])
def test_specification_formats_and_score(query):
    dims=parse_dimensions(query)
    product=SimpleNamespace(length_mm=Decimal(800),width_mm=Decimal(600),height_mm=Decimal(200))
    assert dimension_score(dims,product)==100
    product.length_mm=Decimal(810)
    assert 99 < dimension_score(dims,product) < 100
    assert parse_dimensions('Z.001.000151') is None
    assert parse_dimensions('0×600') is None


def test_all_customer_scope_ranking_before_limit_and_explicit_choice(floor3_app):
    app,ids,factory=floor3_app
    with factory() as db:
        for number, product_id in enumerate(ids['products']):
            product=db.get(Product,product_id)
            product.length_mm=Decimal(810+number*10);product.width_mm=600;product.height_mm=200
        exact=db.get(Product,ids['other_product'])
        exact.length_mm=800;exact.width_mm=600;exact.height_mm=200
        db.get(Customer,ids['other']).chinese_short_name='其他简称'
        db.commit()
    with TestClient(app) as client:
        _login(client,'floor3-admin')
        response=client.get('/api/warehouse/floor3/product-candidates',params={'q':'800*600*200','limit':1})
        assert response.status_code==200,response.text
        data=response.json();item=data['items'][0]
        assert item['product_id']==ids['other_product']
        assert item['customer_id']==ids['other'] and item['customer_short_name']=='其他简称'
        assert item['match_score']==100 and item['is_exact']
        assert data['auto_bind_allowed'] is False and data['selection_required'] is True
        scoped=client.get('/api/warehouse/floor3/product-candidates',params={'q':'800×600×200','customer_id':ids['tianhua']})
        assert ids['other_product'] not in [row['product_id'] for row in scoped.json()['items']]
        assert [row['match_score'] for row in scoped.json()['items']]==sorted([row['match_score'] for row in scoped.json()['items']],reverse=True)
        _login(client,'floor3-scoped')
        scoped=client.get('/api/warehouse/floor3/product-candidates',params={'q':'800×600×200'})
        assert scoped.status_code==200,scoped.text
        assert ids['other_product'] not in [row['product_id'] for row in scoped.json()['items']]


def test_flat_dimensions_missing_height_and_inactive_products(floor3_app):
    app,ids,factory=floor3_app
    with factory() as db:
        product=db.get(Product,ids['other_product']);product.length_mm=800;product.width_mm=600;product.height_mm=None
        inactive=db.get(Product,ids['products'][0]);inactive.is_active=False
        db.commit()
    with TestClient(app) as client:
        _login(client,'floor3-admin')
        for query,found in [('800×600',True),('800×600×200',False)]:
            response=client.get('/api/warehouse/floor3/product-candidates',params={'q':query})
            product_ids=[row['product_id'] for row in response.json()['items']]
            assert (ids['other_product'] in product_ids)==found
            assert ids['products'][0] not in product_ids

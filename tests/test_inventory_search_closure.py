from datetime import date
from fastapi.testclient import TestClient
from sqlalchemy import select
import pytest
from tests.test_product_processed_source_output import seed_output, mobile_erp_app, _app, _login
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services.warehouse_search_paging import search_lot_page
from app.services.warehouse_product_search import catalog_without_stock
from app.services.warehouse_twin_dashboard import build_inventory_code_search_results
from app.services.inventory_product_sources import source_products


@pytest.mark.parametrize('moved',[False,True])
def test_home_and_warehouse_find_completed_source_and_same_location(mobile_erp_app,moved):
    app,ids,factory=_app(mobile_erp_app)
    with factory() as db:
        lid,location_id,pid=seed_output(db,ids,moved=moved)
        product=db.get(Product,pid);product.product_code='Z.001.000012';product.customer_material_code='Z.001.000012';db.commit()
        found,page=search_lot_page(db,select(InventoryLot).where(InventoryLot.id==lid),keyword='00012',as_of=date.today())
        assert [row.id for row in found]==[lid]
        result=build_inventory_code_search_results(lots=found,keyword='00012',as_of=date.today())['items'][0]
        assert result['location_id']==location_id and result['employee_location_name']
        assert result['source_products'][0]['product_id']==pid
        assert pid not in [row['product_id'] for row in catalog_without_stock(db,keyword='00012',visible_customer_ids=None)['items']]
        assert source_products(db,[lid],{db.get(Product,ids['other_product']).customer_id})=={}
    with TestClient(app) as client:
        _login(client,'mobile-admin')
        response=client.get('/api/product-workbench/search',params={'q':'00012'})
        assert response.status_code==200,response.text
        card=next(row for row in response.json()['items'] if row['id']==pid)
        position=next(row for row in card['inventory_positions'] if row['lot_id']==lid)
        assert position['location_id']==location_id and position['location_label']
        assert position['actual']==30 and position['unit']=='片'
        assert not position['mapped'] and position['map_issue']


def test_renamed_finished_code_keeps_old_stock_location(mobile_erp_app):
    app,ids,factory=_app(mobile_erp_app)
    with factory() as db:
        product=db.get(Product,ids['product']);product.product_code='CURRENT-00012';db.commit()
        from app.services.product_workbench import inventory_details
        details=inventory_details(db,product)
        assert details['summary']['finished']['actual']>0
        lots,_=search_lot_page(db,select(InventoryLot),keyword='CURRENT-00012',as_of=date.today())
        assert any(row.finished_detail and row.finished_detail.product_id==product.id for row in lots)
        expected={row['lot_id'] for row in details['items'] if row['inventory_type']=='finished'}
    with TestClient(app) as client:
        _login(client,'mobile-admin')
        card=client.get('/api/product-workbench/search',params={'q':'CURRENT-00012'}).json()['items'][0]
        assert expected <= {row['lot_id'] for row in card['inventory_positions']}
        assert card['inventory']['finished']['actual']>0

from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_stock_replenishment_flow import (
    stock_replenishment_app, _login, _customer_replenishment_payload,
)


def test_selected_products_without_policy_save_once_and_read_back(stock_replenishment_app):
    from app.models.product import Product
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot
    app, factory = stock_replenishment_app
    with factory() as db:
        source = db.get(Product, 1)
        second = Product(customer_id=1, product_code='SECOND', customer_material_code='SECOND', product_name='虚构第二款',
            material_id=source.material_id, layer_count=5, flute_type='AB',
            report_length_mm=1865, report_width_mm=830, crease_type='压线',
            crease_left_mm=335, crease_middle_mm=160, crease_right_mm=335)
        db.add(second); db.commit(); second_id = second.id
        before = list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available)))
        assert db.scalar(select(func.count()).select_from(InventoryStockPolicy)) == 0
    with TestClient(app) as client:
        _login(client)
        response = client.get('/api/requisition/stock-replenishment/products',
            params=[('customer_id',1),('product_ids',1),('product_ids',second_id),('limit',1)])
        assert response.status_code == 200, response.text
        assert {row['id'] for row in response.json()['items']} == {1, second_id}
        payload = _customer_replenishment_payload(quantity=12)
        line = payload['items'][0]
        line['reference_product_id'] = line.pop('product_id'); line['location_id'] = None
        second_line = deepcopy(line); second_line.update(reference_product_id=second_id, quantity=25)
        payload['items'].append(second_line)
        created = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert created.status_code == 201, created.text
        data = created.json()
        assert data['status'] == 'draft'
        read = client.get(f"/api/requisition/stock-replenishment/orders/{data['id']}")
        assert read.status_code == 200, read.text
        assert {(row['reference_product_id'], row['quantity']) for row in read.json()['items']} == {(1,12),(second_id,25)}
        replay = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert replay.status_code == 201 and replay.json()['id'] == data['id']
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StockReplenishmentOrder)) == 1
        assert db.scalar(select(func.count()).select_from(InventoryStockPolicy)) == 0
        assert list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available))) == before


def test_exact_selection_retains_customer_active_and_permission_gates(stock_replenishment_app):
    from app.models.product import Product
    from app.models.customer import Customer
    from app.models.user import User
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    app, factory = stock_replenishment_app
    with factory() as db:
        other = Customer(customer_number=2, customer_code='OTHER', name='虚构范围外客户')
        db.add(other); db.flush()
        foreign = Product(customer_id=other.id, product_code='21301010', customer_material_code='21301010', product_name='同编码不同客户')
        inactive = Product(customer_id=1, product_code='INACTIVE', customer_material_code='INACTIVE', product_name='停用', is_active=False)
        db.add_all([foreign,inactive]); db.flush(); foreign_id, inactive_id = foreign.id, inactive.id
        user = db.get(User,1); user.role='sales'; user.customer_access_mode='selected'
        db.add(UserCustomerScope(user_id=1,customer_id=1))
        db.add(UserPermissionOverride(user_id=1,permission_code='requisition.view',is_allowed=True))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        route='/api/requisition/stock-replenishment/products'
        response=client.get(route,params=[('product_ids',1),('product_ids',foreign_id),('product_ids',inactive_id)])
        assert response.status_code==200, response.text
        assert [row['id'] for row in response.json()['items']]==[1]
        assert client.get(route,params={'customer_id':2,'product_ids':foreign_id}).status_code==403
        assert client.get(route,params={'product_ids':0}).status_code==422
        assert client.get(route,params=[('product_ids',i+1) for i in range(101)]).status_code==422
        with factory() as db:
            override=db.scalar(select(UserPermissionOverride));override.is_allowed=False;db.commit()
        assert client.get(route,params={'product_ids':1}).status_code==403


def test_order_entries_load_the_picker_and_consume_link_after_session_restore():
    from pathlib import Path
    html=Path('static/index.html').read_text(encoding='utf-8')
    assert html.count('<manual-replenishment-picker v-if="canRequisition"')==2
    assert 'selectedOrderReplenishmentLink()' in html
    assert 'await window.TMManualReplenishment.openFromLocation(this)' in html

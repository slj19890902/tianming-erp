"""Actual authenticated API calls over fictional customers and one shared lot."""
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models.user import User
from app.models.product import Product
from app.models.material import Material
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.models.access_control import UserPermissionOverride
from app.models.order import OrderItem
from app.services.finished_stock_identity import product_basis
from app.core.security import hash_password
from tests.test_p1_18_pdf_inventory_contract import inventory_preview_app, _login, _preview_item, _finished_plan, PASSWORD


@pytest.fixture()
def shared_api(inventory_preview_app):
    app,factory,ids=inventory_preview_app
    from app.api.warehouse import router as warehouse
    from app.api.deliveries import router as deliveries
    app.include_router(warehouse,prefix='/api/warehouse')
    app.include_router(deliveries,prefix='/api/deliveries')
    with factory() as db:
        from app.models.supplier import Supplier
        from app.services.supplier_master import normalize_supplier_identity
        db.add(Supplier(standard_name='虚构供应商',display_name='虚构供应商',
            normalized_name=normalize_supplier_identity('虚构供应商'),is_active=True,version=1,sort_order=10))
        source=db.get(Product,ids['shared_product'])
        material=Material(code='FICTION-MAT',supplier_name='虚构供应商',layer_count=3,flute_type='B',
            quote_price=2,price_unit='元/㎡',purchase_currency='CNY',purchase_tax_included=True,is_active=True)
        db.add(material);db.flush()
        source.material_id=material.id
        source.sale_unit_price=Decimal('1')
        target=Product(customer_id=ids['other_customer'],product_code='B-CODE',customer_material_code='B-MATERIAL',
            product_name='虚构乙客户产品',box_category=source.box_category,box_style=source.box_style,
            material_id=material.id,legacy_material_text=source.legacy_material_text,flute_type='B',layer_count=3,
            report_length_mm=800,report_width_mm=600,splice_mode='single',pieces_per_box=1,sale_unit_price=9)
        admin=User(username='shared-admin',password_hash=hash_password(PASSWORD),role='admin',
            real_name='虚构管理员',must_change_password=False)
        db.add_all([target,admin]);db.flush()
        lot=db.get(InventoryLot,ids['shared_lot'])
        lot.finished_detail.physical_basis_json=product_basis(source)
        lot.estimated_unit_cost_snapshot=Decimal('1')
        for user in db.scalars(select(User).where(User.role=='sales')):
            for code in ('warehouse.reserve','orders.create','deliveries.create','deliveries.execute','deliveries.view'):
                db.add(UserPermissionOverride(user_id=user.id,permission_code=code,is_allowed=True))
        db.commit()
        ids={**ids,'target':target.id}
    return app,factory,ids


def confirm(client,ids):
    request=dict(product_ids=[ids['shared_product'],ids['target']],lot_ids=[ids['shared_lot']])
    preview=client.post('/api/warehouse/finished/shared-stock/preview',json=request)
    assert preview.status_code==200,preview.text
    response=client.post('/api/warehouse/finished/shared-stock/confirm',json={**request,
        'preview_hash':preview.json()['preview_hash'],'operation_key':'fiction-shared',
        'physical_match_confirmed':True,'evidence':'虚构样品，现场确认双方均可使用'})
    assert response.status_code==200,response.text
    return preview.json()['lots'][0]['version']+1


def test_customer_scoped_candidates_draft_save_readback_dispatch_and_cancel(shared_api):
    app,factory,ids=shared_api
    with TestClient(app) as client:
        _login(client,'preview-other-customer')
        request=dict(product_ids=[ids['shared_product'],ids['target']],lot_ids=[ids['shared_lot']])
        assert client.post('/api/warehouse/finished/shared-stock/preview',json=request).status_code==403
        _login(client,'shared-admin');version=confirm(client,ids)
        _login(client,'preview-other-customer')
        candidates=client.get(f"/api/warehouse/finished/products/{ids['target']}/candidates",
            params={'customer_id':ids['other_customer']})
        assert candidates.status_code==200,candidates.text
        assert len(candidates.json()['items'])==1
        candidate=candidates.json()['items'][0]
        assert candidate['shared_stock'] and candidate['inventory_code']=='B-CODE'
        assert 'P1-18匿名客户A' not in candidates.text
        assert 'sale_unit_price' not in candidates.text
        plan={**_finished_plan(ids['shared_lot'],5),'expected_version':version}
        item=_preview_item(line='shared-B',product_id=ids['target'],quantity=5,finished=[plan])
        preview=client.post('/api/orders/inventory-draft-preview',json={
            'customer_id':ids['other_customer'],'items':[item]})
        assert preview.status_code==200,preview.text
        assert preview.json()['items'][0]['finished_planned_quantity']==5
        assert preview.json()['items'][0]['production_required_quantity']==0
        from app.core.time_contract import beijing_today
        request=dict(customer_id=ids['other_customer'],customer_po='FICTION-SHARED-PO',
            idempotency_key='fiction-shared-order',order_date=beijing_today().isoformat(),
            items=[{**item,'unit_price':'9'}])
        saved=client.post('/api/orders',json=request)
        assert saved.status_code==201,saved.text
        oid=saved.json()['id'];iid=saved.json()['items'][0]['id']
        assert client.get(f'/api/orders/{oid}').status_code==200
        with factory() as db:
            lot=db.get(InventoryLot,ids['shared_lot'])
            assert (lot.quantity_available,lot.quantity_reserved)==(0,5)
            assert db.get(OrderItem,iid).unit_price==9
            assert lot.finished_detail.product_id==ids['shared_product']
        delivery=client.post('/api/deliveries',json=dict(customer_id=ids['other_customer'],
            delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=iid,delivered_quantity=5)]))
        assert delivery.status_code==201,delivery.text
        did=delivery.json()['id']
        dispatched=client.put(f'/api/deliveries/{did}/dispatch')
        assert dispatched.status_code==200,dispatched.text
        cancelled=client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code==200,cancelled.text
        with factory() as db:
            lot=db.get(InventoryLot,ids['shared_lot'])
            assert (lot.quantity_reserved,lot.quantity_consumed)==(5,0)
        # The other customer's order remains outside the viewer's scope.
        _login(client,'preview-allowed')
        assert client.get(f'/api/orders/{oid}').status_code==403


def test_unrelated_product_cannot_take_confirmed_lot(shared_api):
    app,factory,ids=shared_api
    with TestClient(app) as client:
        _login(client,'shared-admin');version=confirm(client,ids)
        _login(client,'preview-allowed')
        plan={**_finished_plan(ids['shared_lot'],2),'expected_version':version}
        response=client.post('/api/orders/inventory-draft-preview',json=dict(customer_id=ids['customer'],
            items=[_preview_item(line='unrelated',product_id=ids['none_product'],quantity=2,finished=[plan])]))
        assert response.status_code==409,response.text
    with factory() as db:
        assert db.get(InventoryLot,ids['shared_lot']).quantity_available==5
        assert list(db.scalars(select(InventoryReservation)))==[]

from types import SimpleNamespace as NS

from app.services import product_specification as specs


def product(**values):
    return NS(**{**dict(length_mm=None,width_mm=None,height_mm=None,
        supply_mode='corrugated_production',external_packaging_specification_summary=None),**values})


def test_generated_mold_identity_is_not_a_finished_specification():
    old='模切成品 80011906；模具 M-60E649C761DE；1模'
    assert specs.resolved_order_specification(old,product()) is None
    assert specs.resolved_order_specification(old,product(length_mm=430,width_mm=718)) == '430×718mm'


def test_valid_frozen_spec_is_preserved_even_if_master_changes():
    assert specs.resolved_order_specification('345×182×246mm',product(length_mm=999,width_mm=888)) == '345×182×246mm'
    assert specs.resolved_order_specification('客户约定规格 Ø72',product()) == '客户约定规格 Ø72'


def test_blank_spec_never_calls_warehouse_identity_resolver(monkeypatch):
    def forbidden(*args,**kwargs): raise AssertionError('warehouse resolver must not supply order size')
    monkeypatch.setattr(specs,'product_stock_specification',forbidden)
    assert specs.resolved_order_specification(None,product()) is None
    assert specs.resolved_order_specification('-',product(length_mm=570,width_mm=325)) == '570×325mm'
    assert specs.resolved_order_specification('待完善',product()) is None


def test_location_uses_only_short_position_or_raw_code(monkeypatch):
    from app.services import mold_location as order_specs
    monkeypatch.setattr(order_specs,'describe_mold_location',lambda value: {'short_label':'R04','prompt':'请前往模具架；三模；其他提示'})
    assert order_specs.order_mold_location('MCELL-123') == 'R04'
    monkeypatch.setattr(order_specs,'describe_mold_location',lambda value: {'prompt':'请前往位置'})
    assert order_specs.order_mold_location(' R04 ') == 'R04'
    assert order_specs.order_mold_location(None) == ''


def test_order_list_projection_cleans_legacy_spec_and_never_rewrites_storage(n029_delivery_app):
    from fastapi.testclient import TestClient
    from test_n029_production_integration import _login
    from app.models.order import OrderItem
    from app.models.product import Product
    app,factory,ids=n029_delivery_app
    old='模切成品 80011906；模具 M-60E649C761DE；1模'
    with factory() as db:
        item=db.get(OrderItem,ids['task_completed'])
        p=db.get(Product,item.product_id)
        p.length_mm=p.width_mm=p.height_mm=None
        item.snapshot_spec=old
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response=client.get('/api/orders',params={'page_size':100})
        assert response.status_code==200,response.text
        rows=[i for order in response.json()['items'] for i in order['items']]
        row=next(i for i in rows if i['id']==ids['task_completed'])
        assert row['snapshot_spec'] is None
    with factory() as db:
        assert db.get(OrderItem,ids['task_completed']).snapshot_spec==old


# Metadata-only disposable fixture; never a copy of the formal database.
from test_n029_production_integration import n029_delivery_app


def test_new_order_saves_clean_spec_and_legacy_po_duplicate_still_rejected(n029_delivery_app):
    from fastapi.testclient import TestClient
    from test_n029_production_integration import _login
    from app.models.order import OrderItem
    from app.models.product import Product
    app,factory,ids=n029_delivery_app
    old='模切成品 80011906；模具 M-60E649C761DE；1模'
    with factory() as db:
        p=db.get(Product,ids['product_legacy_received'])
        p.length_mm=p.width_mm=p.height_mm=None
        db.commit()
    payload={'customer_id':ids['customer'],'customer_po':'SPEC-CLEAN-PO',
        'order_date':'2026-10-17','import_integrity_status':'ok','items':[
        {'client_line_id':'SPEC-CLEAN-1','product_id':ids['product_legacy_received'],
         'quantity':100,'unit_price':'1.00','specification':old}]}
    with TestClient(app) as client:
        _login(client)
        response=client.post('/api/orders',json=payload)
        assert response.status_code==201,response.text
        item=response.json()['items'][0]
        assert item['snapshot_spec'] is None
        with factory() as db:
            saved=db.get(OrderItem,item['id'])
            assert saved.snapshot_spec is None
            saved.snapshot_spec=old
            db.commit()
        repeated=client.post('/api/orders',json=payload)
        assert repeated.status_code==409,repeated.text
        edited=client.put(f"/api/orders/items/{item['id']}",json={
            'quantity':100,'unit_price':'1.00','product_code':item['snapshot_product_code'],
            'product_name':item['snapshot_product_name'],'material':item.get('snapshot_material'),
            'specification':old,'sync_product':False})
        assert edited.status_code==200,edited.text
        assert edited.json()['snapshot_spec'] is None
        with factory() as db:
            assert db.get(OrderItem,item['id']).snapshot_spec is None

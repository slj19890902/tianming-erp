import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from tests.test_incoming_actual_specs import purchase
from test_stock_replenishment_flow import stock_replenishment_app
from app.models.product import Product
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.warehouse_inventory import InventoryLot


def request_for(client, factory, identity):
    with factory() as db:
        p = db.get(Product, 1)
        p.report_length_mm, p.report_width_mm = 1865, 830
        p.sheet_cutting_settings = {'schema_version':2,'whole':{
            'length_parts':1,'width_parts':1,'mold_count':1,'is_die_cut':False}}
        db.commit()
    catalog = client.get(f'/api/incoming/actual-materials/sr{identity}').json()
    material = next(m for m in catalog['materials'] if m['id']==catalog['material_id'])
    return dict(material_id=material['id'],material_version=material['version'],
        length_mm=1865,width_mm=2490,supplier_edge_type='净料',sync_supplier_defaults=True,
        expected_product_version=catalog['product_version'])


def test_actual_supplier_size_sync_preserves_theoretical_and_original_purchase(stock_replenishment_app):
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        identity = purchase(client, factory)
        request = request_for(client, factory, identity)
        r = client.post(f'/api/incoming/actual-preview/sr{identity}',json=request)
        assert r.status_code == 200, r.text
        body = dict(received_quantity=67, actual_spec=dict(request,fingerprint=r.json()['fingerprint']),
            idempotency_key='supplier-default-sync')
        saved = client.put(f'/api/incoming/receive/sr{identity}',json=body)
        assert saved.status_code == 200, saved.text
        replay = client.put(f'/api/incoming/receive/sr{identity}',json=body)
        assert replay.status_code == 200, replay.text
        history = client.get('/api/incoming/history',params={'board_width_mm':2490}).json()['items']
        assert history and history[0]['supplier_edge_type'] == '净料'
    with factory() as db:
        p = db.get(Product, 1)
        assert (p.report_length_mm,p.report_width_mm)==(1865,830)
        assert p.sheet_cutting_settings['whole']['actual_supplier_width_mm']=='2490'
        assert p.sheet_cutting_settings['whole']['width_parts']==3
        assert p.supplier_edge_type=='净料'
        assert p.crease_type=='压线'
        assert p.version == request['expected_product_version']+1
        from app.services.stock_replenishment import product_replenishment_defaults
        assert product_replenishment_defaults(p)['report_width_mm'] == 2490
        assert db.scalar(select(func.count(IncomingReceiptItem.id)))==1
        source = db.get(StockReplenishmentOrderItem, identity)
        original = json.loads(source.sheet_cutting_snapshot)
        assert str(original['supplier_width_mm'])=='830'


def test_changed_master_after_preview_blocks_entire_receipt(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        identity = purchase(client, factory)
        request = request_for(client, factory, identity)
        preview = client.post(f'/api/incoming/actual-preview/sr{identity}',json=request)
        assert preview.status_code==200,preview.text
        with factory() as db:
            db.get(Product,1).version+=1
            db.commit()
        response=client.put(f'/api/incoming/receive/sr{identity}',json=dict(received_quantity=67,
            actual_spec=dict(request,fingerprint=preview.json()['fingerprint']),idempotency_key='stale-defaults'))
        assert response.status_code==409,response.text
    with factory() as db:
        assert db.scalar(select(func.count(IncomingReceiptItem.id)))==0
        assert db.get(Product,1).sheet_cutting_settings['schema_version']==2


def test_ba_is_same_flute_as_ab_without_changing_original_material():
    from app.services.supplier_material_display import clean_supplier_flute_type
    assert clean_supplier_flute_type(' BA ')==clean_supplier_flute_type('AB')=='AB'
    assert clean_supplier_flute_type('BE')=='BE'

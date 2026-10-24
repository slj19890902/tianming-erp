"""Synthetic paper/print jobs: never open a formal business database."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.services.product_label_media import ProductLabelPrintMedia, normalize_print_media, bind_print_media
from app.models.product import Product
from app.models.production_label_print import ProductionPackagingLabelPrintJob as PackagingJob
from app.models.finished_product_label import FinishedProductLabelPrintJob as FinishedJob
from app.models.audit import OperationLog
from tests.test_p1_32a2_requisition_production_print import production_print_app, _login
from tests.test_p1_50c_task_label_plan_template import _enable_one_frozen_plan, _write_fact_snapshot
from tests.test_p1_21b_mobile_admin_product_search import mobile_erp_app
from tests.test_finished_product_labels_20261019 import setup as finished_setup, preview as finished_preview, body as finished_body, inventory as finished_inventory, ROOT as FINISHED_ROOT
from tests.test_supplier_stock_labels_20261016 import stock_replenishment_app, reported, preview as supplier_preview, request as supplier_request
from tests.test_p1_106_composite_delivery_labels import delivery_label_api


@pytest.mark.parametrize('preset,width,height,rows,columns', [
    ('roll_40x30', 40, 30, 1, 1), ('roll_40x80', 40, 80, 1, 1),
    ('roll_80x40', 80, 40, 1, 1), ('roll_65x45', 65, 45, 1, 1),
    ('a4_4up', 105, 148.5, 2, 2),
])
def test_preset_geometry_is_identical_on_round_trip(preset, width, height, rows, columns):
    media = normalize_print_media(dict(preset=preset))
    assert (media['label_width_mm'], media['label_height_mm'], media['rows'], media['columns']) == (width, height, rows, columns)
    assert normalize_print_media(media) == media
    assert json.dumps(normalize_print_media(media), sort_keys=True) == json.dumps(media, sort_keys=True)
    assert media['schema_version'] == 1
    assert bind_print_media({'legacy': True}, None) == {'legacy': True}


def test_a4_margins_gaps_start_position_and_flooring_do_not_exceed_paper():
    media = normalize_print_media(dict(preset='a4_4up', margin_top_mm=3, margin_bottom_mm=4,
        margin_left_mm=5.333, margin_right_mm=6, gap_x_mm=2, gap_y_mm=3, start_position=4))
    assert media['label_width_mm'] == 98.333
    assert media['label_height_mm'] == 143.5
    assert media['label_width_mm'] * 2 + 2 + 5.333 + 6 <= 210
    assert media['start_position'] == 4
    custom = normalize_print_media(dict(preset='custom', width_mm=300, height_mm=420))
    assert custom['label_width_mm'] == 300


@pytest.mark.parametrize('bad', [
    {'preset':'unknown'}, {'preset':'custom'},
    {'preset':'custom', 'width_mm':True, 'height_mm':40},
    {'preset':'custom', 'width_mm':'40', 'height_mm':40},
    {'preset':'custom', 'width_mm':float('nan'), 'height_mm':40},
    {'preset':'custom', 'width_mm':float('inf'), 'height_mm':40},
    {'preset':'custom', 'width_mm':19.9, 'height_mm':40},
    {'preset':'custom', 'width_mm':301, 'height_mm':40},
    {'preset':'roll_40x30','width_mm':80},
    {'preset':'roll_40x30','margin_left_mm':21},
    {'preset':'roll_40x30','margin_top_mm':-1},
    {'preset':'roll_40x30','gap_x_mm':1},
    {'preset':'roll_40x30','start_position':2},
    {'preset':'a4_4up','start_position':0},
    {'preset':'a4_4up','start_position':5},
    {'preset':'a4_4up','start_position':True},
    {'preset':'a4_4up','schema_version':True},
    {'preset':'a4_4up','margin_left_mm':float('nan')},
    {'preset':'a4_4up','gap_x_mm':51},
    {'preset':'a4_4up','extra':1},
])
def test_invalid_geometry_rejected_at_api_model(bad):
    with pytest.raises(ValidationError):
        ProductLabelPrintMedia.model_validate(bad)


def task_request(fixture, task, package, key):
    return dict(idempotency_key=key, confirmed=True, plan_fingerprint=package['plan_fingerprint'],
        items=[dict(production_task_id=task, print_label_count=package['label_count'])])


def test_task_media_is_frozen_hashed_and_replay_does_not_reprice_or_change_business(production_print_app):
    f=production_print_app
    task, _, _ = _enable_one_frozen_plan(f)
    before = _write_fact_snapshot(f['session_factory'])
    root=f"/api/requisition/supplier-orders/{f['supplier_order_id']}/production-packaging-label"
    with TestClient(f['app']) as c:
        _login(c, 'p132a2-admin')
        catalog=c.get('/api/requisition/product-label-media')
        assert catalog.status_code==200, catalog.text
        assert {x['preset'] for x in catalog.json()['presets']} >= {'a4_4up','roll_40x80'}
        p=c.get(root+'-package',params={'item_ids':str(f['supplier_item_id'])}).json()
        request=task_request(f,task,p,'media-task');request['print_media']={'preset':'a4_4up','start_position':3}
        r=c.post(root+'-jobs',json=request);assert r.status_code==200,r.text
        data=r.json();job=data['job_id']
        assert data['package']['labels']==p['labels']
        assert data['package']['print_media']['start_position']==3
        assert c.post(root+'-jobs',json=request).json()==data
        changed=copy.deepcopy(request);changed['print_media']['start_position']=1
        assert c.post(root+'-jobs',json=changed).status_code==409
        invalid=copy.deepcopy(request);invalid['print_media']['width_mm']=True
        assert c.post(root+'-jobs',json=invalid).status_code==422
        r=c.post(f'/api/requisition/production-packaging-label-jobs/{job}/confirm',json={'idempotency_key':'media-confirm','confirmed':True})
        assert r.status_code==200,r.text
    after=_write_fact_snapshot(f['session_factory'])
    for key in before:
        if key in ('orders','items','tasks','supplier_orders','completions','refreshes'):
            assert after[key]==before[key],key


def test_historical_packaging_media_variant_keeps_frozen_content_and_links_and_rejects_bad_replay(production_print_app):
    from app.models.production_label_print import ProductionPackagingLabelPrintJobTask as Link
    f=production_print_app;task,_,_=_enable_one_frozen_plan(f)
    root=f"/api/requisition/supplier-orders/{f['supplier_order_id']}/production-packaging-label"
    with TestClient(f['app']) as c:
        _login(c,'p132a2-admin')
        p=c.get(root+'-package',params={'item_ids':str(f['supplier_item_id'])}).json()
        r=c.post(root+'-jobs',json=task_request(f,task,p,'old-label'));assert r.status_code==200,r.text
        old=r.json()['job_id']
        body=dict(jobs=[dict(kind='packaging',id=old)],print_media={'preset':'roll_40x80'},idempotency_key='media-reprint',reprint_confirmed=True)
        assert c.post('/api/requisition/product-label-media/reprints',json=body).status_code==409
        r=c.post(f'/api/requisition/production-packaging-label-jobs/{old}/confirm',json={'idempotency_key':'old-printed','confirmed':True});assert r.status_code==200,r.text
        old_package=r.json()['package']
        with f['session_factory']() as db:
            product=db.get(Product,f['product_id']);product.product_name='后来的主档名称';product.version+=1;db.commit()
        r=c.post('/api/requisition/product-label-media/reprints',json=body);assert r.status_code==200,r.text
        new=r.json()['jobs'][0]['job_id']
        assert new!=old and r.json()['jobs'][0]['kind']=='packaging'
        assert r.json()['package']['labels']==old_package['labels']
        assert r.json()['package']['print_media']['label_height_mm']==80
        assert c.post('/api/requisition/product-label-media/reprints',json=body).json()['replayed'] is True
        conflict=copy.deepcopy(body);conflict['print_media']={'preset':'a4_4up'}
        assert c.post('/api/requisition/product-label-media/reprints',json=conflict).status_code==409
        r=c.get(f'/api/requisition/production-packaging-label-jobs/{new}');assert r.status_code==200,r.text
        printed=c.post(f'/api/requisition/production-packaging-label-jobs/{new}/confirm',json={'idempotency_key':'variant-printed','confirmed':True})
        assert printed.status_code==200,printed.text
        assert printed.json()['status']=='printed'
        original=c.get(f'/api/requisition/production-packaging-label-jobs/{old}')
        assert original.json()['package']==old_package
        with f['session_factory']() as db:
            original_links=list(db.scalars(select(Link).where(Link.print_job_id==old)))
            new_links=list(db.scalars(select(Link).where(Link.print_job_id==new)))
            assert [x.snapshot_json for x in original_links]==[x.snapshot_json for x in new_links]
            assert len(list(db.scalars(select(OperationLog).where(OperationLog.action_code=='production.product_label_media.reprint_prepared'))))==1


def test_finished_media_variant_can_reprint_frozen_lot_after_current_stock_changes(mobile_erp_app):
    from app.api.requisition import router as requisition_router
    from tests.test_p1_21b_mobile_admin_product_search import _login as finished_login
    from app.models.warehouse_inventory import InventoryLot
    app,ids,factory,lot=finished_setup(mobile_erp_app)
    app.include_router(requisition_router,prefix='/api/requisition')
    with TestClient(app) as c:
        finished_login(c,'mobile-admin');p=finished_preview(c,lot)
        body=finished_body(p);body['print_media']={'preset':'roll_80x40'}
        r=c.post(FINISHED_ROOT+'/jobs',json=body);assert r.status_code==200,r.text
        old=r.json()['job_id']
        r=c.post(f'{FINISHED_ROOT}/jobs/{old}/confirm',json=dict(confirmed=True,idempotency_key='finished-old-print'));assert r.status_code==200,r.text
        with factory() as db:
            stock=db.get(InventoryLot,lot);stock.quantity_available=1;stock.version+=1;db.commit()
        before=finished_inventory(factory)
        request=dict(jobs=[dict(kind='finished',id=old)],print_media={'preset':'a4_4up','start_position':4},idempotency_key='finished-new-media',reprint_confirmed=True)
        r=c.post('/api/requisition/product-label-media/reprints',json=request);assert r.status_code==200,r.text
        new=r.json()['jobs'][0]['job_id'];assert new!=old
        assert r.json()['package']['labels']==p['labels']
        r=c.get(f'{FINISHED_ROOT}/jobs/{new}');assert r.status_code==200,r.text
        r=c.post(f'{FINISHED_ROOT}/jobs/{new}/confirm',json=dict(confirmed=True,idempotency_key='finished-new-print'))
        assert r.status_code==200,r.text
        assert r.json()['job']['status']=='printed'
        assert finished_inventory(factory)==before


def test_supplier_stock_source_accepts_same_media_contract(stock_replenishment_app):
    app,factory=stock_replenishment_app
    with TestClient(app) as c:
        order,item,_=reported(c,factory)
        p=supplier_preview(c,order,item)
        body=supplier_request(p);body['print_media']={'preset':'roll_40x80'}
        r=c.post(f'/api/requisition/supplier-orders/{order}/production-packaging-label-jobs',json=body)
        assert r.status_code==200,r.text
        assert r.json()['package']['print_media']['label_height_mm']==80
        assert r.json()['package']['labels']==p['labels']


def test_stock_receipt_media_uses_same_contract_and_preserves_real_source(stock_replenishment_app):
    from tests.test_stock_processing_auto import configure_finished_location
    from tests.test_stock_receipt_labels_20261015 import setup, preview, body, inventory
    app,factory=stock_replenishment_app
    configure_finished_location(factory)
    with TestClient(app) as c:
        receipt=setup(c,factory);p=preview(c,receipt);before=inventory(factory)
        request=body(p);request['print_media']={'preset':'a4_4up','margin_left_mm':5,'margin_right_mm':5,'gap_x_mm':2}
        r=c.post(f'/api/incoming/receipt-items/{receipt}/product-label-jobs',json=request)
        assert r.status_code==200,r.text
        frozen=r.json()['package'];job=r.json()['job_id']
        assert frozen['print_media']['label_width_mm']==99
        assert frozen['source_evidence']==p['source_evidence']
        r=c.post(f'/api/requisition/production-packaging-label-jobs/{job}/confirm',json=dict(confirmed=True,idempotency_key='media-receipt-print'))
        assert r.status_code==200,r.text
        assert inventory(factory)==before


def test_batch_media_is_on_all_children_and_different_media_same_key_fails(stock_replenishment_app):
    app,factory=stock_replenishment_app
    with TestClient(app) as c:
        first=reported(c,factory,23);second=reported(c,factory,12)
        ids=[first[0],second[0]]
        r=c.get('/api/requisition/supplier-order-label-batches/package',params=dict(
            order_ids=','.join(map(str,ids)),item_ids=f'{first[1]},{second[1]}'))
        assert r.status_code==200,r.text
        request=supplier_request(r.json(),key='media-batch',count=1)
        request.update(order_ids=ids,print_media={'preset':'a4_4up','start_position':3})
        path='/api/requisition/supplier-order-label-batches/production-packaging-label-jobs'
        r=c.post(path,json=request);assert r.status_code==200,r.text
        assert r.json()['package']['print_media']['start_position']==3
        assert all(j['package']['print_media']['start_position']==3 for j in r.json()['jobs'])
        assert c.post(path,json=request).json()['replayed'] is True
        request['print_media']['start_position']=1
        assert c.post(path,json=request).status_code==409
        with factory() as db:
            assert len(list(db.scalars(select(PackagingJob))))==2


def test_delivery_media_freezes_without_production_task_links(delivery_label_api):
    from tests.test_p1_106_composite_delivery_labels import _login as delivery_login
    f=delivery_label_api
    root=f"/api/deliveries/{f['delivery_id']}/production-packaging-label"
    with TestClient(f['app']) as c:
        delivery_login(c,'p1106-admin')
        p=c.get(root+'-package').json()
        request=dict(idempotency_key='delivery-media-job',confirmed=True,plan_fingerprint=p['plan_fingerprint'],
            items=[dict(selection_key=p['plans'][0]['selection_key'],print_label_count=2)],
            print_media={'preset':'roll_65x45'})
        r=c.post(root+'-jobs',json=request);assert r.status_code==200,r.text
        assert r.json()['package']['print_media']['label_width_mm']==65
        assert r.json()['package']['label_count']==2
        assert c.post(root+'-jobs',json=request).json()['job_id']==r.json()['job_id']


def test_media_reprint_authorization_and_audit_failure_leave_no_partial_job(production_print_app,monkeypatch):
    from app.api import requisition
    from fastapi import HTTPException
    f=production_print_app;task,_,_=_enable_one_frozen_plan(f)
    root=f"/api/requisition/supplier-orders/{f['supplier_order_id']}/production-packaging-label"
    with TestClient(f['app'],raise_server_exceptions=False) as c:
        _login(c,'p132a2-admin')
        p=c.get(root+'-package',params={'item_ids':str(f['supplier_item_id'])}).json()
        r=c.post(root+'-jobs',json=task_request(f,task,p,'permission-old-label'));assert r.status_code==200,r.text
        old=r.json()['job_id']
        r=c.post(f'/api/requisition/production-packaging-label-jobs/{old}/confirm',json=dict(idempotency_key='permission-old-print',confirmed=True));assert r.status_code==200,r.text
        request=dict(jobs=[dict(kind='packaging',id=old)],print_media={'preset':'roll_40x80'},idempotency_key='permission-media',reprint_confirmed=True)
        with monkeypatch.context() as m:
            def deny(*args,**kwargs):raise HTTPException(403,'outside customer scope')
            m.setattr(requisition,'_require_packaging_label_job_access',deny)
            assert c.post('/api/requisition/product-label-media/reprints',json=request).status_code==403
        with monkeypatch.context() as m:
            def fail(*args,**kwargs):raise RuntimeError('synthetic audit failure')
            m.setattr(requisition,'append_audit_event',fail)
            assert c.post('/api/requisition/product-label-media/reprints',json=request).status_code==500
        with f['session_factory']() as db:
            assert len(list(db.scalars(select(PackagingJob))))==1
        r=c.post('/api/requisition/product-label-media/reprints',json=request);assert r.status_code==200,r.text

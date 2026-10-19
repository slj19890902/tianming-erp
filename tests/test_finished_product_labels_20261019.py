import copy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.api.finished_product_labels import router
from app.models.customer import Customer
from app.models.product import Product
from app.models.finished_product_label import FinishedProductLabelPrintJob as Job
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from app.models.audit import OperationLog
from app.models.access_control import UserPermissionOverride
from app.models.user import User
from test_product_stock_origins import add_lot
from tests.test_p1_21b_mobile_admin_product_search import mobile_erp_app, _login

ROOT='/api/production/finished-product-labels'


def setup(mobile_erp_app):
    app, ids, factory = mobile_erp_app
    app.include_router(router, prefix='/api/production')
    with factory() as db:
        p=db.get(Product,ids['product_two']);p.production_label_enabled=True;p.production_label_units_per_label=6
        db.get(Customer,p.customer_id).chinese_short_name='隔离研光'
        loc=db.scalar(select(InventoryLot.warehouse_location_id))
        lot=add_lot(db,p,loc,'FINISHED-LABEL-1',20,'production_completion','production_completion')
        lot.finished_detail.physical_basis_json=json.dumps(dict(product_id=p.id,unit='pieces',spec='冻结旧规格',assembly=[]))
        lot_id=lot.id
        db.commit()
    return app,ids,factory,lot_id


def preview(c,lot,version=1):
    r=c.get(ROOT+'/package',params={'lots':f'{lot}:{version}'})
    assert r.status_code==200,r.text
    return r.json()['package']


def body(p,key='finished-label-job'):
    return dict(idempotency_key=key,plan_fingerprint=p['plan_fingerprint'],
        selections=[dict(lot_id=e['lot_id'],expected_version=e['expected_version']) for e in p['lot_versions']],
        items=[dict(selection_key=s['selection_key'],print_label_count=s['label_count']) for s in p['plans']])


def inventory(factory):
    with factory() as db:
        return {m.__tablename__:list(db.execute(select(*m.__table__.columns).order_by(*m.__table__.primary_key.columns)))
            for m in (InventoryLot,InventoryMovement,InventoryReservation,Product)}


def test_actual_finished_quantity_tail_label_frozen_job_idempotency_and_no_stock_writes(mobile_erp_app):
    app,ids,factory,lot=setup(mobile_erp_app)
    with TestClient(app) as c:
        _login(c,'mobile-admin');before=inventory(factory)
        p=preview(c,lot);assert [l['quantity'] for l in p['labels']]==[6,6,6,2]
        assert p['plans'][0]['specification']=='冻结旧规格'
        request=body(p);request['items'][0]['print_label_count']=2
        r=c.post(ROOT+'/jobs',json=request);assert r.status_code==200,r.text
        job=r.json()['job_id'];assert r.json()['package']['label_count']==2
        assert c.post(ROOT+'/jobs',json=request).json()['replayed'] is True
        bad=copy.deepcopy(request);bad['items'][0]['print_label_count']=1
        assert c.post(ROOT+'/jobs',json=bad).status_code==409
        path=f'{ROOT}/jobs/{job}/confirm';confirmation=dict(idempotency_key='finished-confirm',confirmed=True)
        r=c.post(path,json=confirmation);assert r.status_code==200,r.text
        assert r.json()['job']['status']=='printed'
        assert c.post(path,json=confirmation).json()['replayed'] is True
        assert c.post(path,json={**confirmation,'idempotency_key':'other'}).status_code==409
        assert inventory(factory)==before
        with factory() as db:
            assert len(list(db.scalars(select(OperationLog).where(OperationLog.action_code.in_([
                'production.finished_product_label.prepared','production.finished_product_label.printed'])))))==2
            prod=db.get(Product,ids['product_two']);prod.product_name='更改后的主档';prod.version+=1;db.commit()
        old=c.get(f'{ROOT}/jobs/{job}');assert old.status_code==200,old.text
        assert old.json()['package']['plans'][0]['product_name']!= '更改后的主档'
        new=preview(c,lot);req=body(new,'reprint')
        assert c.post(ROOT+'/jobs',json=req).status_code==409
        req['reprint_confirmed']=True
        assert c.post(ROOT+'/jobs',json=req).status_code==200


@pytest.mark.parametrize('change',['version','quantity','label_policy','basis','disabled','damaged','semi','stocktake'])
def test_stale_or_ineligible_finished_stock_is_rejected_atomically(mobile_erp_app,change):
    app,ids,factory,lot=setup(mobile_erp_app)
    with TestClient(app) as c:
        _login(c,'mobile-admin');p=preview(c,lot)
        with factory() as db:
            l=db.get(InventoryLot,lot);prod=db.get(Product,ids['product_two'])
            if change=='version':l.version+=1
            elif change=='quantity':l.quantity_available=19
            elif change=='label_policy':prod.production_label_units_per_label=10
            elif change=='basis':l.finished_detail.physical_basis_json='{}'
            elif change=='disabled':prod.production_label_enabled=False;prod.production_label_units_per_label=None
            elif change=='damaged':l.quantity_damaged=1
            elif change=='semi':l.inventory_type='semi_finished'
            elif change=='stocktake':l.source_ref_type=None;l.source_type='stocktake'
            db.commit()
        r=c.post(ROOT+'/jobs',json=body(p));assert r.status_code==409,r.text
        with factory() as db:assert db.scalar(select(Job.id)) is None


def test_bom_completed_parent_and_standalone_child_use_actual_stock_identity(mobile_erp_app):
    app,ids,factory,lot=setup(mobile_erp_app)
    with factory() as db:
        parent=db.get(Product,ids['product']);parent.production_label_enabled=True;parent.production_label_units_per_label=3
        child=db.get(Product,ids['product_two']);child.is_internal_component=True
        l=db.get(InventoryLot,lot);loc=l.warehouse_location_id
        assembled=add_lot(db,parent,loc,'ASSEMBLED-PARENT',7,'production_completion','bom_assembly')
        assembled.finished_detail.physical_basis_json=json.dumps(dict(product_id=parent.id,unit='套',spec='',assembly=[dict(product_id=child.id,quantity=2,basis_hash='a'*64)]))
        parent_lot=assembled.id;db.commit()
    with TestClient(app) as c:
        _login(c,'mobile-admin')
        r=c.get(ROOT+'/package',params={'lots':f'{parent_lot}:1,{lot}:1'});assert r.status_code==200,r.text
        p=r.json()['package'];assert len(p['plans'])==2
        by_product={p['product_id']:p for p in p['plans']}
        assert by_product[ids['product']]['unit_label']=='套'
        assert by_product[ids['product']]['total_quantity']==7
        assert by_product[ids['product']]['label_quantities']==[3,3,1]
        assert by_product[ids['product_two']]['total_quantity']==20
        assert by_product[ids['product_two']]['unit_label']=='片'
        assert c.post(ROOT+'/jobs',json=body(p)).status_code==200


def test_permission_scope_duplicate_selection_and_confirmation_stale_guard(mobile_erp_app):
    app,ids,factory,lot=setup(mobile_erp_app)
    with TestClient(app) as c:
        assert c.get(ROOT+'/package',params={'lots':f'{lot}:1'}).status_code==401
        _login(c,'mobile-admin');p=preview(c,lot)
        assert c.get(ROOT+'/package',params={'lots':f'{lot}:1,{lot}:1'}).status_code==409
        req=body(p);req['items']*=2;assert c.post(ROOT+'/jobs',json=req).status_code==422
        r=c.post(ROOT+'/jobs',json=body(p));job=r.json()['job_id']
        with factory() as db:
            db.get(InventoryLot,lot).version+=1;db.commit()
        assert c.get(f'{ROOT}/jobs/{job}').status_code==409
        assert c.post(f'{ROOT}/jobs/{job}/confirm',json=dict(idempotency_key='stale',confirmed=True)).status_code==409
        with factory() as db:assert db.get(Job,job).status=='prepared'
        _login(c,'mobile-scoped')
        with factory() as db:
            u=db.scalar(select(User).where(User.username=='mobile-scoped'))
            db.add(UserPermissionOverride(user_id=u.id,permission_code='orders.view',is_allowed=False));db.commit()
        assert c.get(ROOT+'/package',params={'lots':f'{lot}:2'}).status_code==403


def test_prepare_and_confirmation_audit_failure_roll_back(mobile_erp_app,monkeypatch):
    app,ids,factory,lot=setup(mobile_erp_app)
    import app.api.finished_product_labels as api
    with TestClient(app,raise_server_exceptions=False) as c:
        _login(c,'mobile-admin');p=preview(c,lot)
        real=api.append_audit_event
        def fail(*a,**k):raise RuntimeError('isolated audit failure')
        monkeypatch.setattr(api,'append_audit_event',fail)
        assert c.post(ROOT+'/jobs',json=body(p)).status_code==500
        with factory() as db:assert db.scalar(select(Job.id)) is None
        monkeypatch.setattr(api,'append_audit_event',real)
        r=c.post(ROOT+'/jobs',json=body(p));assert r.status_code==200,r.text
        job=r.json()['job_id'];monkeypatch.setattr(api,'append_audit_event',fail)
        assert c.post(f'{ROOT}/jobs/{job}/confirm',json=dict(idempotency_key='audit-confirm',confirmed=True)).status_code==500
        with factory() as db:assert db.get(Job,job).status=='prepared'


def test_all_customer_scope_and_mixed_disabled_are_atomic(mobile_erp_app):
    app,ids,factory,lot=setup(mobile_erp_app)
    with factory() as db:
        other=db.get(Product,ids['other_product']);other.production_label_enabled=True;other.production_label_units_per_label=6
        db.get(Customer,other.customer_id).chinese_short_name='范围外客户'
        loc=db.get(InventoryLot,lot).warehouse_location_id
        outside=add_lot(db,other,loc,'OUTSIDE-LABEL',5,'production_completion','production_completion')
        outside.finished_detail.physical_basis_json=json.dumps(dict(product_id=other.id,unit='pieces',spec='外部冻结',assembly=[]))
        outside_id=outside.id;db.commit()
    with TestClient(app) as c:
        _login(c,'mobile-admin');p=preview(c,outside_id)
        r=c.post(ROOT+'/jobs',json=body(p));assert r.status_code==200,r.text
        outside_job=r.json()['job_id']
        _login(c,'mobile-scoped')
        assert c.get(ROOT+'/package',params={'lots':f'{outside_id}:1'}).status_code==404
        assert c.get(ROOT+'/package',params={'lots':f'{lot}:1,{outside_id}:1'}).status_code==404
        assert c.post(ROOT+'/jobs',json=body(p,'outside-forbidden')).status_code==404
        assert c.get(f'{ROOT}/jobs/{outside_job}').status_code==404
        assert c.post(f'{ROOT}/jobs/{outside_job}/confirm',json=dict(idempotency_key='forbidden-confirm',confirmed=True)).status_code==404
        _login(c,'mobile-admin')
        both=c.get(ROOT+'/package',params={'lots':f'{lot}:1,{outside_id}:1'}).json()['package']
        with factory() as db:
            other=db.get(Product,ids['other_product']);other.production_label_enabled=False;other.production_label_units_per_label=None;db.commit()
        assert c.post(ROOT+'/jobs',json=body(both,'mixed-disabled')).status_code==409
        with factory() as db:assert len(list(db.scalars(select(Job.id))))==1

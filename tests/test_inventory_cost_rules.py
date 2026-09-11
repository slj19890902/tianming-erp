from datetime import date
from decimal import Decimal
import json
import pytest
from sqlalchemy import select, func
from app.models.inventory_cost_rule import InventoryCostRule, InventoryCostMutation
from app.models.user import User
from app.models.warehouse_inventory import WarehouseLocation, InventoryLot
from app.services import inventory_cost_rules as service
from app.services.inventory_valuation import resolve_product_cost
from app.services.warehouse_inventory import manual_finished_in
from tests.test_inventory_valuation import product
from tests.test_inventory_cost_snapshot import db


def setup(db):
    p,m=product(db)
    user=User(username='cost-rule-admin',real_name='验收',password_hash='test-only',role='admin',is_active=True)
    location=WarehouseLocation(location_code='RULE',location_name='成本验证',warehouse_type='finished')
    db.add_all([user,location]);db.flush()
    return p,m,user,location


def save(db,p,user,config,version=0):
    return service.save_rule(db,p,config,user=user,expected_version=version,expected_product_version=p.version)


def lot(db,p,user,loc,key='entry',source='stocktake'):
    return manual_finished_in(db,customer_id=p.customer_id,product_id=p.id,location_id=loc.id,quantity=10,
        stock_date=date(2026,9,11),source_type=source,remarks=None,operator_id=user.id,idempotency_key=key)


def test_material_rule_mm_yield_flute_freeze_and_production_facts_unchanged(db):
    p,m,u,loc=setup(db);m.quote_price=Decimal('1.64')
    config=dict(mode='material',material_id=m.id,length_mm=1140,width_mm=900,products_per_sheet=2,flute_type='E',basis='用户指定一开二')
    save(db,p,u,config)
    l=lot(db,p,u,loc)
    assert l.estimated_unit_cost_snapshot==Decimal('.8413')
    assert p.report_length_mm is None and p.box_style=='A1'
    m.quote_price=Decimal('3.28');db.flush()
    assert lot(db,p,u,loc).estimated_unit_cost_snapshot==Decimal('.8413')
    new=lot(db,p,u,loc,'entry2');assert new.estimated_unit_cost_snapshot==Decimal('1.6826')
    assert db.scalar(select(func.count()).select_from(InventoryCostRule))==1


def test_temporary_centimetres_confirmed_as_1000_mm_not_production_dimensions(db):
    p,m,u,loc=setup(db);m.quote_price=Decimal('1.13')
    save(db,p,u,dict(mode='material',material_id=m.id,length_mm=1000,width_mm=1000,basis='临时100cm',temporary=True))
    e=resolve_product_cost(db,p).estimate
    assert e.unit_cost==Decimal('1.1300') and e.area_m2==1
    assert p.length_mm==500 and p.report_length_mm is None


def test_positive_sale_fallback_and_material_precedence(db):
    p,m,u,loc=setup(db);p.sale_unit_price=13
    assert resolve_product_cost(db,p).estimate.unit_cost==Decimal('1.6300')
    m.quote_price=None;db.flush()
    e=resolve_product_cost(db,p).estimate
    assert e.unit_cost==13 and e.detail['cost_label']=='售价参考成本'
    l=lot(db,p,u,loc);p.sale_unit_price=14;db.flush()
    assert l.estimated_unit_cost_snapshot==13
    p.sale_unit_price=0;db.flush();assert resolve_product_cost(db,p).estimate is None


def test_physical_kit_rule_never_adds_child_cost_twice(db):
    p,m,u,loc=setup(db);p.is_composite=True;p.sale_unit_price=13
    save(db,p,u,dict(mode='fixed',unit_cost='1.667',basis='箱体分摊，整套13元'))
    assert lot(db,p,u,loc).estimated_unit_cost_snapshot==Decimal('1.6670')
    # A similarly coded product/customer receives no rule through code matching.
    assert resolve_product_cost(db,p).estimate.detail['product_id']==p.id


def test_rule_version_audit_and_existing_stock_requires_explicit_preview(db):
    p,m,u,loc=setup(db);l=lot(db,p,u,loc);db.commit()
    save(db,p,u,dict(mode='fixed',unit_cost=7,basis='合作厂含税开票价'));db.commit()
    assert l.estimated_unit_cost_snapshot==Decimal('1.6300')
    with pytest.raises(ValueError,match='已被修改'):save(db,p,u,dict(mode='sale',basis='变价'))
    db.rollback()
    plan=service.preview_revalue(db,p,[l.id]);old_version=l.version
    result=service.revalue(db,p,[l.id],user=u,expected=plan['fingerprint'],batch_id='update-one');db.commit()
    assert l.estimated_unit_cost_snapshot==7 and l.version==old_version+1
    assert l.quantity_available==10 and l.warehouse_location_id==loc.id
    assert service.revalue(db,p,[l.id],user=u,expected=plan['fingerprint'],batch_id='update-one')['replayed']
    assert db.get(InventoryCostMutation,'update-one').response_json==service.canonical(result)
    assert json.loads(l.cost_snapshot_detail_json)['original_cost']['estimated_unit_cost_snapshot']=='1.6300'
    with pytest.raises(ValueError,match='更换请求'):
        service.revalue(db,p,[l.id],user=u,expected='0'*64,batch_id='update-one')


def test_stale_quote_inventory_and_failed_audit_are_atomic(db,monkeypatch):
    p,m,u,loc=setup(db);l=lot(db,p,u,loc);db.commit()
    plan=service.preview_revalue(db,p,[l.id]);m.quote_price=3;db.flush()
    with pytest.raises(ValueError,match='已变化'):
        service.revalue(db,p,[l.id],user=u,expected=plan['fingerprint'],batch_id='stale')
    db.rollback()
    plan=service.preview_revalue(db,p,[l.id])
    monkeypatch.setattr(service,'append_audit_event',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('audit failed')))
    with pytest.raises(RuntimeError,match='audit'):
        service.revalue(db,p,[l.id],user=u,expected=plan['fingerprint'],batch_id='audit')
    db.rollback();db.refresh(l)
    assert l.version==plan['rows'][0]['version']
    assert db.get(InventoryCostMutation,'audit') is None


def test_actual_purchase_production_and_untraceable_transfer_cannot_be_revalued(db):
    p,m,u,loc=setup(db);l=lot(db,p,u,loc)
    for source,ref in [('production_completion','production_completion'),('transfer',None),('stocktake','external_packaging_receipt_item')]:
        l.source_type=source;l.source_ref_type=ref;db.flush()
        with pytest.raises(ValueError,match='不属于'):
            service.preview_revalue(db,p,[l.id])


@pytest.mark.parametrize('role',['finance','workshop','warehouse','sales','boss'])
def test_write_role_is_admin_only(db,role):
    p,m,u,loc=setup(db);u.role=role
    with pytest.raises(PermissionError):save(db,p,u,dict(mode='fixed',unit_cost=2,basis='test'))
    with pytest.raises(PermissionError):service.revalue(db,p,[1],user=u,expected='0'*64,batch_id='no')


def test_cost_api_roles_scope_and_stale_rule(db,monkeypatch):
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    from app.api import inventory_cost_rules as api
    from app.api.deps import get_db,get_current_user
    p,m,u,loc=setup(db);db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api/warehouse')
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[get_current_user]=lambda:u
    monkeypatch.setattr(api,'require_customer_access',lambda cid,user,session:None)
    with TestClient(app) as client:
        for role in ['finance','workshop','sales']:
            u.role=role
            assert client.get(f'/api/warehouse/cost-rules/{p.id}').status_code==403
            assert client.get('/api/warehouse/cost-rules/materials').status_code==403
        u.role='admin'
        r=client.get(f'/api/warehouse/cost-rules/{p.id}')
        assert r.status_code==200 and 'no-store' in r.headers['cache-control']
        endpoint=f'/api/warehouse/cost-rules/{p.id}'
        payload=dict(expected_version=0,expected_product_version=p.version,config=dict(mode='fixed',unit_cost=5,basis='含税参考'))
        write=client.put(endpoint,json=payload)
        assert write.status_code==200,write.text
        assert client.put(endpoint,json=payload).status_code==409
        u.role='boss'
        assert client.get(endpoint).status_code==200
        assert client.put(endpoint,json={**payload,'expected_version':1}).status_code==403
        u.role='admin'
        def deny(*a,**kw):raise HTTPException(403,'客户范围')
        monkeypatch.setattr(api,'require_customer_access',deny)
        assert client.get(f'/api/warehouse/cost-rules/{p.id}').status_code==403


def test_manifest_rejects_other_customer_and_is_atomic(db,monkeypatch):
    from scripts.admin.warehouse_cost_rules import preview_manifest,adopt_manifest
    p,m,u,loc=setup(db);l=lot(db,p,u,loc);db.commit()
    item=dict(product_id=p.id,product_code=p.product_code,product_name=p.product_name,customer_id=p.customer_id,
        product_version=p.version,config=dict(mode='fixed',unit_cost=3,basis='文件含税'),lot_ids=[l.id])
    manifest=dict(authorization='用户指定产品和价格',unmatched=[],rules=[item])
    bad={**manifest,'rules':[{**item,'customer_id':p.customer_id+1}]}
    with pytest.raises(ValueError,match='身份'):preview_manifest(db,bad)
    plan=preview_manifest(db,manifest)
    original=service.append_audit_event
    monkeypatch.setattr(service,'append_audit_event',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('audit')))
    with pytest.raises(RuntimeError):adopt_manifest(db,manifest,expected=plan['fingerprint'],batch='manifest',user=u)
    db.rollback()
    assert db.get(InventoryCostRule,p.id) is None and l.estimated_unit_cost_snapshot==Decimal('1.6300')
    monkeypatch.setattr(service,'append_audit_event',original)
    assert adopt_manifest(db,manifest,expected=plan['fingerprint'],batch='manifest',user=u)['lot_count']==1
    db.commit()
    assert adopt_manifest(db,manifest,expected=plan['fingerprint'],batch='manifest',user=u)['replayed']

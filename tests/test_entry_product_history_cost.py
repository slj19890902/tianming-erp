from decimal import Decimal
from types import SimpleNamespace as NS
import pytest
from fastapi.testclient import TestClient
from tests.test_inventory_cost_snapshot import db
from tests.test_inventory_valuation import product
from tests.test_fin001_invoice_tasks import fin001_app, _login


def test_current_recipe_requires_explicit_identity_and_version(db):
    from app.services.material_cost_supplement import _approved_current_reference
    p,material=product(db,report_length_mm=1000,report_width_mm=500)
    gap=dict(reason='no_delivery_cost_source',quantity=2,
             item=NS(product_id=p.id,order_item_id=None),source={})
    ref,error=_approved_current_reference(db,gap,p.version)
    assert error is None and ref['unit_cost']==Decimal('1.0000')
    assert ref['reference_kind']=='approved_current_product_material_reference'
    assert _approved_current_reference(db,gap,p.version+1)[0] is None
    assert _approved_current_reference(db,{**gap,'source':{'lot':NS(finished_detail=NS(product_id=p.id+1))}},p.version)[0] is None
    assert _approved_current_reference(db,{**gap,'source':{'kind':'subkit'}},p.version)[0] is None
    material.quote_price=None;p.sale_unit_price=99
    assert _approved_current_reference(db,gap,p.version)[0] is None  # never use sale as material evidence


def test_entry_editor_reuses_product_revision_and_preserves_price(fin001_app):
    from app.api.products import router as products
    from app.api.inventory_cost_rules import router as costs
    from app.models.product import Product
    app,factory=fin001_app
    app.include_router(products,prefix='/api/master/products')
    app.include_router(costs,prefix='/api/warehouse')
    with factory() as db:
        p=db.get(Product,1);p.box_style='A1';p.production_process='无需结合';p.sale_unit_price=7
        p.length_mm=500;p.width_mm=300;p.height_mm=200;p.report_length_mm=1630;p.report_width_mm=500
        db.commit()
    with TestClient(app) as c:
        _login(c,'fin001-finance')
        assert c.get('/api/warehouse/cost-rules/1/entry-preview').status_code==403
        _login(c,'fin001-admin')
        before=c.get('/api/master/products/1').json()
        payload={**before,'expected_version':before['version'],'report_length_mm':1640,'change_reason':'入仓前核对规格'}
        preview=c.post('/api/master/products/1/update-preview',json=payload)
        assert preview.status_code==200,preview.text
        payload['confirmation_token']=preview.json()['confirmation_token']
        result=c.put('/api/master/products/1',json=payload)
        assert result.status_code==200,result.text
        after=result.json()
        assert after['sale_unit_price']==before['sale_unit_price']
        assert after['cost_unit_price']==before['cost_unit_price']
        assert after['report_length_mm']==1640 and after['version']==before['version']+1
        assert c.put('/api/master/products/1',json=payload).status_code==409
        cost=c.get('/api/warehouse/cost-rules/1/entry-preview')
        assert cost.status_code==200,cost.text
        assert cost.json()['product_version']==after['version']


def test_sales_bulk_approval_replay_survives_audit_mapping_limit(fin001_app):
    from sqlalchemy import select
    from app.models.user import User
    from app.services import historical_sales_contract as service
    _,factory=fin001_app
    with factory() as db:
        actor=db.scalar(select(User).where(User.role=='admin'))
        units={10000+i:'只' for i in range(70)}
        plan=service.preview(db,['2026-09'],confirmed_tax_inclusive=True,approved_units=units)
        kw=dict(months=['2026-09'],actor=actor,expected_preview=plan['preview_fingerprint'],
            batch_id='70-confirmed-units',reason='老板逐类核对',confirmed_tax_inclusive=True,approved_units=units)
        service.adopt(db,**kw);db.commit()
        assert service.adopt(db,**kw)['replayed']
        with pytest.raises(ValueError):service.adopt(db,**{**kw,'approved_units':{**units,10069:'套'}})

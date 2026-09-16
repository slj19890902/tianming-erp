from datetime import date
from decimal import Decimal
import json
import pytest
from sqlalchemy import select, func
from tests.test_inventory_cost_snapshot import db, _customer, _material
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
from app.models.processing_cost import ProcessingCostSettings, ProductProcessingProfile
from app.models.warehouse_inventory import WarehouseLocation, InventoryLot, InventoryMovement
from app.services.inventory_valuation import resolve_product_cost, freeze_entry_cost, cost_payload
from app.services.warehouse_inventory import manual_finished_in
from tests.test_p1_131_processing_cost import processing_cost_app, _login


def seed(db):
    c = _customer(db)
    m = _material(db, code='K8K', price='1.8')
    m.purchase_currency='CNY';m.purchase_tax_included=True;m.purchase_tax_rate=Decimal('.13')
    parent=Product(customer_id=c.id, product_code='160008',customer_material_code='160008',product_name='支架成套',
        box_style='016 井字架',unit='套',is_composite=True,combination_mode='parent_priced_set')
    db.add(parent);db.flush()
    db.add(ProductBomProfile(product_id=parent.id,source='assembled',material_mode='expand_children',delivery_mode='parent'))
    children=[]
    for label,q,y in [('A',5,16),('B',9,15),('C',1,8)]:
        p=Product(customer_id=c.id,product_code=label,customer_material_code=label,product_name=label,box_style='隔板',
            box_category='normal',unit='只',material_id=m.id,report_length_mm=700,report_width_mm=950,
            layer_count=3,flute_type='B',pieces_per_box=1,default_cutting_mode=f'一开{y}')
        db.add(p);db.flush();children.append(p)
        e=ProductBomComponent(parent_product_id=parent.id,component_product_id=p.id,
            quantity_per_set=q,display_order=len(children),internal_component_code=label,is_required=True)
        db.add(e);db.flush();db.add(ProductBomInventoryRelation(bom_component_id=e.id,relation='assembly'))
    settings=db.get(ProcessingCostSettings,1)
    settings.average_worker_monthly_salary=5500
    settings.average_worker_monthly_social_cost=0
    location=WarehouseLocation(location_code='BOM-COST',location_name='成套位',warehouse_type='finished')
    db.add(location);db.flush()
    return parent,children,m,location


def test_assembled_stocktake_cost_yield_and_replay(db):
    p,children,m,location=seed(db)
    result=resolve_product_cost(db,p)
    assert not result.missing, result.missing
    # Each child is valued per physical piece, rounded to the ledger precision.
    expected=sum((Decimal('1.197')/Decimal(y)).quantize(Decimal('.0001'))*q for y,q in [(16,5),(15,9),(8,1)])
    assert result.estimate.unit_cost == expected
    assert result.estimate.detail['standard_labour_unit_cost']=='0.5288'
    before=db.scalar(select(func.count()).select_from(InventoryMovement))
    args=dict(customer_id=p.customer_id,product_id=p.id,location_id=location.id,quantity=10,
        stock_date=date(2026,9,16),source_type='stocktake',remarks=None,operator_id=None,idempotency_key='assembled-test')
    lot=manual_finished_in(db,**args)
    db.flush()
    assert db.scalar(select(func.count()).select_from(InventoryMovement))==before+1
    assert cost_payload(lot,db)['standard_labour_unit_cost']=='0.5288'
    assert lot.estimated_unit_cost_snapshot==expected
    m.quote_price=99
    again=manual_finished_in(db,**args)
    assert again.id==lot.id and again.estimated_unit_cost_snapshot==expected


def test_slower_assembly_changes_labour_not_material(db):
    p,children,m,location=seed(db)
    a=resolve_product_cost(db,p).estimate
    db.add(ProductProcessingProfile(product_id=p.id,assembly_worker_days_per_1000=5))
    db.flush()
    b=resolve_product_cost(db,p).estimate
    assert a.unit_cost==b.unit_cost
    assert b.detail['standard_labour_unit_cost']=='1.0577'


def test_child_missing_cost_identifies_child_not_parent(db):
    p,children,m,location=seed(db)
    children[0].report_length_mm=None;children[0].report_width_mm=None
    r=resolve_product_cost(db,p)
    assert r.estimate is None
    assert any('A' in s for s in r.missing)


def test_open_sheet_yield_flows_into_order_plan_and_parent_pick(db):
    from app.models.order import OrderItem
    from app.services.multilevel_bom_compile import compile_master_order_bom
    from app.services.multilevel_bom_plan import plan_bom
    p,children,m,location=seed(db)
    compiled=compile_master_order_bom(db,OrderItem(id=99,order_id=99,product_id=p.id,quantity=10,unit_price=1,subtotal=10))
    plan=plan_bom(compiled.graph,10)
    assert [(s.snapshot_component_default_cutting_mode) for s in compiled.snapshots if s.component_product_id!=p.id]==['一开16','一开15','一开8']
    rows={r.product_id:r for r in plan.materials}
    assert [(rows[c.id].required_pieces,rows[c.id].purchase_sheets) for c in children]==[(50,4),(90,6),(10,2)]
    assert dict(plan.picking)=={p.id:10}
    # Physical child balances are pieces; open-sheet factors do not change BOM ratios.
    assert [e.quantity for e in compiled.graph.edges]==[5,9,1]
    children[0].default_cutting_mode='一开一'
    assert plan_bom(compiled.graph,10).materials==plan.materials


def test_assembly_rate_payload_and_settings_cas(db):
    from app.api.processing_cost import ProductProcessingProfileFields, _profile_values
    from fastapi import HTTPException
    seed(db)
    settings=db.get(ProcessingCostSettings,1)
    payload=ProductProcessingProfileFields(assembly_output_per_person_hour=25,assembly_settings_version=settings.version)
    assert _profile_values(payload,db)['assembly_worker_days_per_1000']==Decimal(5)
    settings.version+=1
    with pytest.raises(HTTPException) as e:_profile_values(payload,db)
    assert e.value.status_code==409
    for rate in [0,-1,'NaN','Infinity']:
        with pytest.raises(ValueError):ProductProcessingProfileFields(assembly_output_per_person_hour=rate,assembly_settings_version=1)


def test_unassembled_ordinary_parent_still_requires_own_material(db):
    p,children,m,location=seed(db)
    db.get(ProductBomProfile,p.id).source='manufactured'
    r=resolve_product_cost(db,p,main_only=True)
    assert r.estimate is None
    assert any('材质' in s for s in r.missing)


def test_rate_api_replay_and_stale_version(processing_cost_app):
    from fastapi.testclient import TestClient
    app,factory=processing_cost_app
    with TestClient(app) as client:
        _login(client)
        settings=client.get('/api/finance/processing-settings').json()
        profile=client.get('/api/finance/product-processing-profiles/1').json()
        payload=dict(printer_mode=profile['printer_mode'],die_cut_mode=profile['die_cut_mode'],
            assembly_output_per_person_hour='25',assembly_settings_version=settings['version'],
            expected_version=profile['version'],idempotency_key='bom438-hour-rate')
        r=client.put('/api/finance/product-processing-profiles/1',json=payload)
        assert r.status_code==200,r.text
        assert Decimal(r.json()['assembly_worker_days_per_1000'])==5
        replay=client.put('/api/finance/product-processing-profiles/1',json=payload)
        assert replay.status_code==200 and replay.json()['version']==r.json()['version']
        assert Decimal(str(replay.json()['assembly_worker_days_per_1000']))==5
        stale=client.put('/api/finance/product-processing-profiles/1',json={**payload,'idempotency_key':'bom438-hour-stale'})
        assert stale.status_code==409
        conflict=client.put('/api/finance/product-processing-profiles/1',json={**payload,'assembly_output_per_person_hour':'20'})
        assert conflict.status_code==409


def test_nested_set_sums_children_and_own_labour_once(db):
    p,children,m,location=seed(db)
    child_cost=resolve_product_cost(db,p).estimate
    parent=Product(customer_id=p.customer_id,product_code='OUTER',customer_material_code='OUTER',product_name='外层组合',
        unit='套',box_style='BOM组合',is_composite=True)
    db.add(parent);db.flush()
    db.add(ProductBomProfile(product_id=parent.id,source='assembled',material_mode='expand_children',delivery_mode='parent'))
    e=ProductBomComponent(parent_product_id=parent.id,component_product_id=p.id,quantity_per_set=2,display_order=1,is_required=True,internal_component_code='SUB')
    db.add(e);db.flush();db.add(ProductBomInventoryRelation(bom_component_id=e.id,relation='assembly'));db.flush()
    result=resolve_product_cost(db,parent)
    assert not result.missing,result.missing
    assert result.estimate.unit_cost==2*child_cost.unit_cost
    assert result.estimate.detail['standard_labour_unit_cost']=='1.5864'
    cycle=ProductBomComponent(parent_product_id=p.id,component_product_id=parent.id,quantity_per_set=1,display_order=4,is_required=True,internal_component_code='CYCLE')
    db.add(cycle);db.flush();db.add(ProductBomInventoryRelation(bom_component_id=cycle.id,relation='assembly'));db.flush()
    assert any('循环' in s for s in resolve_product_cost(db,parent).missing)

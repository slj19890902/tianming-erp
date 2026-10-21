from datetime import date
from decimal import Decimal
import json
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from app.models.scrap_production import ScrapProductionJob
from app.models.warehouse_inventory import InventoryLot
from app.services import scrap_production as service
from app.services.production_variants import normalize_variants,resolve_variant
from tests.test_fixed_shelf import setup,rack_factory


@pytest.fixture
def reservation_db(setup):
    from app.models.product import Product
    from app.models.user import User
    from app.models.order import Order,OrderItem
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.finished_stock_identity import product_basis
    factory,pid,cid,locations=setup
    with factory() as db:
        p=db.get(Product,pid);p.layer_count=p.material.layer_count;p.flute_type='BE' if p.layer_count==5 else 'B';p.crease_type='毛片'
        order=Order(order_number='SCRAP-FICTION-ORDER',customer_id=cid,order_date=date(2026,10,21),
            status='pending_production',payment_status='unpaid',total_amount=100)
        db.add(order);db.flush()
        basis=json.loads(product_basis(p))
        item=OrderItem(order_id=order.id,product_id=pid,quantity=100,unit_price=1,subtotal=100,
            material_status='pending',requisition_status='未报料',snapshot_product_code=p.product_code,
            snapshot_product_name=p.product_name,snapshot_spec=basis['spec'],snapshot_material=basis['material'],
            snapshot_pieces_per_box=1,snapshot_report_length_mm=p.report_length_mm,
            snapshot_report_width_mm=p.report_width_mm,layer_count=p.layer_count,flute_type=p.flute_type,snapshot_crease_type='毛片')
        db.add(item);db.commit()
        yield db,dict(product=p,item=item,location=db.get(WarehouseLocation,locations[0]),admin=db.get(User,1))


def request(data,**updates):
    return dict(action='plan',product_id=data['product'].id,order_item_id=None,quantity=20,
        location_id=data['location'].id,stock_date='2026-10-21',variant_id=None,remarks='隔离测试',
        operation_key='scrap-test-first',preview_hash='',**updates)


def labour(monkeypatch):
    monkeypatch.setattr('app.services.processing_cost.estimate_standard_processing_cost',
        lambda db,**kw:dict(calculation_status='calculated',estimated_processing_cost='12.34',missing_items=[]))


def save(db,data,payload):
    preview=service.preview(db,payload,data['admin'])
    result=service.mutate(db,{**payload,'preview_hash':preview['preview_hash']},data['admin'])
    db.commit();return result


def test_plan_changes_no_stock_complete_is_idempotent_and_material_zero(reservation_db,monkeypatch):
    db,data=reservation_db;labour(monkeypatch)
    count=db.query(InventoryLot).count()
    result=save(db,data,request(data))
    assert result['status']=='pending' and db.query(InventoryLot).count()==count
    payload={**request(data),'action':'complete','job_id':result['id'],
        'job_version':result['version'],'operation_key':'scrap-test-finish'}
    result=service.mutate(db,payload,data['admin']);db.commit()
    lot=db.get(InventoryLot,result['output_lot_id'])
    assert lot.quantity_available==20 and lot.quantity_reserved==0
    assert lot.estimated_unit_cost_snapshot==Decimal('0.617')
    assert lot.cost_snapshot_source=='scrap_zero_material_frozen_labour'
    assert json.loads(lot.cost_snapshot_detail_json)['material_cost']=='0'
    assert service.mutate(db,payload,data['admin'])['id']==result['id'];db.commit()
    assert db.query(InventoryLot).count()==count+1


def test_order_plan_removes_only_its_gap_and_cancel_restores_it(reservation_db,monkeypatch):
    db,data=reservation_db;labour(monkeypatch)
    from app.api.requisition import _current_requisition_requirements
    item=data['item']
    before=_current_requisition_requirements(db,item)
    payload=request(data);payload['order_item_id']=item.id
    result=save(db,data,payload)
    after=_current_requisition_requirements(db,item)
    assert after['remaining_required_piece_qty']==before['remaining_required_piece_qty']-20*before['pieces_per_box']
    service.mutate(db,{**payload,'action':'cancel','job_id':result['id'],'job_version':result['version'],
        'operation_key':'scrap-test-cancel'},data['admin']);db.commit()
    assert _current_requisition_requirements(db,item)['remaining_required_piece_qty']==before['remaining_required_piece_qty']


def test_direct_finish_reserves_for_original_order_and_does_not_duplicate(reservation_db,monkeypatch):
    db,data=reservation_db;labour(monkeypatch)
    payload=request(data);payload.update(action='finish_now',order_item_id=data['item'].id)
    result=save(db,data,payload)
    lot=db.get(InventoryLot,result['output_lot_id'])
    assert lot.quantity_reserved==20 and lot.quantity_available==0
    assert lot.quantity_consumed==0


def test_missing_cost_and_bom_parent_have_specific_errors(reservation_db,monkeypatch):
    db,data=reservation_db
    monkeypatch.setattr('app.services.processing_cost.estimate_standard_processing_cost',
        lambda db,**kw:dict(calculation_status='incomplete',missing_items=['平均生产月薪待维护']))
    with pytest.raises(HTTPException,match='月薪'):service.preview(db,request(data),data['admin'])
    data['product'].is_composite=True;db.flush()
    with pytest.raises(HTTPException,match='真实消耗子件'):service.preview(db,request(data),data['admin'])
    db.rollback()


def test_failed_audit_rolls_back_finished_inventory(reservation_db,monkeypatch):
    db,data=reservation_db;labour(monkeypatch)
    payload=request(data);payload['action']='finish_now'
    preview=service.preview(db,payload,data['admin'])
    count=db.query(InventoryLot).count()
    def fail(*a,**kw):raise RuntimeError('audit unavailable')
    monkeypatch.setattr('app.services.audit_log.append_audit_event',fail)
    with pytest.raises(RuntimeError):service.mutate(db,{**payload,'preview_hash':preview['preview_hash']},data['admin'])
    db.rollback()
    assert db.query(InventoryLot).count()==count
    assert db.query(ScrapProductionJob).count()==0


def test_mold_count_and_cutting_are_separate_and_invalid_routes_rejected():
    result=normalize_variants([dict(mold_tool_id=369,mold_count=4,length_parts=3,width_parts=1),
        dict(mold_tool_id=210,mold_count=3,length_parts=1,width_parts=1)])
    assert [r['mold_count']*r['length_parts']*r['width_parts'] for r in result]==[12,3]
    with pytest.raises(ValueError):normalize_variants([dict(mold_tool_id=1,mold_count=1.5)])
    with pytest.raises(ValueError):normalize_variants([dict(id='same',mold_tool_id=1),dict(id='same',mold_tool_id=2)])

def test_scrap_cost_is_verified_through_real_location_split_and_rejects_tampering(reservation_db,monkeypatch):
    from tests.test_fixed_shelf import move
    from app.models.warehouse_inventory import WarehouseLocation
    db,data=reservation_db;labour(monkeypatch)
    payload=request(data);payload['action']='finish_now'
    result=save(db,data,payload);lot=db.get(InventoryLot,result['output_lot_id'])
    target=db.scalar(select(WarehouseLocation).where(WarehouseLocation.id!=lot.warehouse_location_id,WarehouseLocation.floor3_layout.has()).order_by(WarehouseLocation.id))
    moved=move(db,lot,target.id,5);db.commit();child=moved.target_lot
    unit,_=service.frozen_cost(db,child,json.loads(child.cost_snapshot_detail_json))
    assert unit==Decimal('0.617') and child.quantity_available==5
    detail=json.loads(child.cost_snapshot_detail_json);detail['material_cost']='1'
    assert service.frozen_cost(db,child,detail)[0] is None
    child.source_ref_id=999999
    assert service.frozen_cost(db,child,json.loads(child.cost_snapshot_detail_json))[0] is None
    db.rollback()

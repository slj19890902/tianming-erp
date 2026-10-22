import json
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.services.sheet_material_display import explain_sheet
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_manual_sheet_deduction_20261013 import seed, BASE
from tests.test_manual_sheet_modal_20261019 import batch_request, change_stock
from tests.test_scrap_production import reservation_db, labour
from tests.test_fixed_shelf import setup, rack_factory


@pytest.mark.parametrize('width,color,score,waste', [(1000,'green',100,0),(1100,'green',90.91,1000),
    (1250,'yellow',80,2500),(1500,'orange',66.67,5000)])
def test_per_sheet_waste_uses_rectangles_not_mold_output(width,color,score,waste):
    result=explain_sheet(dict(cut_plan=dict(source_length_mm=1000,source_width_mm=width,
        target_length_mm=500,target_width_mm=500,rows=2,columns=2,blank_yield=6)))
    assert result['utilization_percent']==score and result['match_color']==color
    assert result['waste_area_cm2']==waste


@pytest.mark.parametrize('plan',[None,{},dict(source_length_mm=0),dict(source_length_mm=100,source_width_mm=100,
    target_length_mm=200,target_width_mm=200,rows=1,columns=1)])
def test_unknown_geometry_never_promises_zero_waste(plan):
    assert explain_sheet({'cut_plan':plan})['waste_area_cm2'] is None


def test_explicit_seventy_sheets_keeps_order_credit_capped_and_replays(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation
    from app.services.semi_finished_inventory import consume_semi_finished_reservation
    app,factory=composite_requisition_app
    lot_id=seed(factory)
    change_stock(factory,lot_id,board_length_mm=2000,board_width_mm=1400)
    with TestClient(app) as client:
        _login(client)
        data=client.get(BASE+'/manual-inventory?match_mode=auto').json()
        row=data['items'][0]
        assert row['max_stock_quantity']==5 and row['actual_max_stock_quantity']==204
        assert row['utilization_percent']==100 and row['waste_area_cm2']==0
        request=batch_request(data,70)
        assert client.post(BASE+'/manual-reserve-batch',json=request).status_code==409
        request['produce_extra_stock']=True
        bad={**request,'items':[{**request['items'][0],'stock_quantity':205}]}
        assert client.post(BASE+'/manual-reserve-batch',json=bad).status_code==409
        response=client.post(BASE+'/manual-reserve-batch',json=request)
        assert response.status_code==200,response.text
        assert response.json()['allocated_requirement_quantity']==20
        assert response.json()['stock_quantity']==70
        assert client.post(BASE+'/manual-reserve-batch',json=request).json()['replayed']
        changed={**request,'items':[{**request['items'][0],'stock_quantity':69}]}
        assert client.post(BASE+'/manual-reserve-batch',json=changed).status_code==409
        rid=response.json()['reservation_ids'][0]
    with factory() as db:
        reservation=db.get(InventoryReservation,rid)
        assert json.loads(reservation.cut_plan_json)['actual_sheet_input_v1'] is True
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(134,70,0)
        consume_semi_finished_reservation(db,reservation_id=rid,stock_quantity=70,
            expected_version=lot.version,operator_id=1,idempotency_key='real-seventy-sheet-input')
        db.commit()
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(134,0,70)


def test_match_ranks_utilization_within_same_size_tier(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot,WarehouseLocation
    app,factory=composite_requisition_app
    ids=[]
    for i,size in enumerate([(2200,1500),(2000,1400),(800,2100)]):
        lot_id=seed(factory)
        change_stock(factory,lot_id,board_length_mm=size[0],board_width_mm=size[1])
        with factory() as db:
            lot=db.get(InventoryLot,lot_id);lot.lot_number=f'SCORE-{i}'
            db.get(WarehouseLocation,lot.warehouse_location_id).location_code=f'SCORE-{i}'
            db.commit()
        ids.append(lot_id)
    with TestClient(app) as client:
        _login(client)
        data=client.get(BASE+'/manual-inventory?match_mode=auto').json()
        assert [row['lot_id'] for row in data['items']]==[ids[1],ids[0],ids[2]]


def combined_request(db,data,monkeypatch):
    from datetime import date,datetime
    from app.models.warehouse_inventory import InventoryLot,SemiFinishedInventoryDetail
    from app.api.warehouse import _order_manual_sheet_state,ManualSheetBatchPayload
    from app.services.scrap_production import preview
    labour(monkeypatch)
    product=data['product'];item=data['item']
    from app.models.receipt_putaway import ProductStoragePreference
    db.add(ProductStoragePreference(product_id=product.id,location_id=data['location'].id,version=1))
    # This is an isolated new physical sheet batch, not a production DB copy.
    lot=InventoryLot(lot_number='COMBINED-SHEET',inventory_type='semi_finished',warehouse_location_id=data['location'].id,
        quantity_available=100,quantity_reserved=0,quantity_consumed=0,quantity_damaged=0,quantity_scrapped=0,
        unit='sheets',status='active',source_type='manual',stock_date=date.today(),last_movement_at=datetime.now())
    db.add(lot);db.flush()
    material=item.snapshot_material
    from app.services.warehouse_inventory import normalize_material_code
    db.add(SemiFinishedInventoryDetail(inventory_lot_id=lot.id,owner_customer_id=None,
        material_code_snapshot=material,normalized_material_code=normalize_material_code(material),
        layer_count=product.layer_count,flute_type=product.flute_type,
        board_length_mm=item.snapshot_report_length_mm,board_width_mm=item.snapshot_report_width_mm,
        component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1,sheet_type='raw_board',internal_name='测试原板'))
    db.commit()
    state=_order_manual_sheet_state(db,item.id,'whole',data['admin'],'auto')[3]
    assert state['scrap_completion_allowed'] and state['items']
    request=batch_request(state,80)
    form=dict(action='finish_now',product_id=product.id,order_item_id=item.id,quantity=20,
        stock_date='2026-10-22',location_id=None,variant_id=None,remarks='库料短缺，已用废料补足')
    checked=preview(db,form,data['admin'])
    request['scrap_completion']=dict(quantity=20,stock_date=form['stock_date'],preview_hash=checked['preview_hash'])
    return lot.id,ManualSheetBatchPayload(**request)


def test_scrap_shortfall_is_atomic_real_finished_stock_and_material_zero(reservation_db,monkeypatch):
    from app.api.warehouse import _reserve_manual_sheet_batch
    from app.models.warehouse_inventory import InventoryLot
    from app.services.inventory_valuation import frozen_cost
    db,data=reservation_db
    lot_id,request=combined_request(db,data,monkeypatch)
    result=_reserve_manual_sheet_batch(data['item'].id,request,db,data['admin'],is_bom=False)
    assert result['remaining_required_piece_qty']==0
    assert result['scrap_completion']['quantity']==20
    finished=db.get(InventoryLot,result['scrap_completion']['output_lot_id'])
    assert (finished.quantity_available,finished.quantity_reserved)==(0,20)
    assert frozen_cost(finished,db)[1]['material_cost']=='0'
    assert frozen_cost(finished,db)[1]['total_cost']=='12.34'
    assert db.get(InventoryLot,lot_id).quantity_reserved==80
    assert data['item'].quantity==100 and data['item'].subtotal==100
    assert _reserve_manual_sheet_batch(data['item'].id,request,db,data['admin'],is_bom=False)['replayed']


def test_failure_after_scrap_creation_rolls_back_every_ledger(reservation_db,monkeypatch):
    from fastapi import HTTPException
    from app.api import warehouse
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation
    from app.models.scrap_production import ScrapProductionJob
    db,data=reservation_db
    lot_id,request=combined_request(db,data,monkeypatch)
    original=db.query(InventoryLot).count()
    def fail(*args,**kwargs):raise warehouse.WarehouseInventoryError('故障注入',409)
    monkeypatch.setattr(warehouse,'_reserve_manual_sheet_inventory',fail)
    with pytest.raises(HTTPException):
        warehouse._reserve_manual_sheet_batch(data['item'].id,request,db,data['admin'],is_bom=False)
    assert db.query(InventoryLot).count()==original
    assert db.get(InventoryLot,lot_id).quantity_available==100
    assert db.query(ScrapProductionJob).count()==0
    assert db.query(InventoryReservation).count()==0


@pytest.mark.parametrize('problem',['quantity','permission','stale_material'])
def test_combined_scrap_rejects_unverified_requests_before_any_write(reservation_db,monkeypatch,problem):
    from fastapi import HTTPException
    from app.api import warehouse
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation
    from app.models.scrap_production import ScrapProductionJob
    db,data=reservation_db
    lot_id,request=combined_request(db,data,monkeypatch)
    if problem=='quantity':request.scrap_completion.quantity=21
    if problem=='permission':monkeypatch.setattr('app.api.deps.has_permission',lambda *args:False)
    if problem=='stale_material':request.items[0].expected_version+=1
    original=db.query(InventoryLot).count()
    with pytest.raises(HTTPException):
        warehouse._reserve_manual_sheet_batch(data['item'].id,request,db,data['admin'],is_bom=False)
    assert db.query(InventoryLot).count()==original
    assert db.get(InventoryLot,lot_id).quantity_available==100
    assert db.query(ScrapProductionJob).count()==db.query(InventoryReservation).count()==0

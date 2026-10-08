import json
import pytest
from decimal import Decimal
from app.models.material import Material
from test_semi_finished_lot_eligibility import eligibility_db, _add_lot, _add_requirement
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.services.sheet_cut_plan import rectangular_cut_plan
from app.services.material_candidates import candidate_items, candidate_response
from app.services.semi_finished_inventory import (
    semi_finished_candidates_for_product, requirement_signature, SemiFinishedLotVersion,
    reserve_semi_finished_inventory, consume_semi_finished_reservation,
    reverse_semi_finished_consumption, release_semi_finished_reservation,
    CUSTOMER_GENERIC_SEMI_FINISHED_STOCK, SIGNATURE_OVERRIDE_WARNING,
)
from app.services.warehouse_inventory import WarehouseInventoryError


def prepare(db, data):
    # New physical entries require a real supplier quotation contract.
    db.add(Material(code='A416D', supplier_name='裁切测试供应商', layer_count=3,
        flute_type='B', quote_price=Decimal('2'), price_unit='元/㎡',
        purchase_currency='CNY', purchase_tax_included=True, purchase_tax_rate=Decimal('0.13')))
    db.flush()
    p=data['products'][0]
    p.box_style='衬板';p.length_mm=p.report_length_mm=1000;p.width_mm=p.report_width_mm=200
    lot=_add_lot(db,data,key='cut-1120',customer_id=None,quantity=6,length=1120)
    lot.semi_finished_detail.board_width_mm=440
    facts=dict(scope='general',customer_ids=[],product_ids=[],processing='cut',material_confidence='confirmed',
        verified_material_id=None,material_code='A416D',face_paper='kraft',mold_tool_id=None)
    profile=WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(facts));db.add(profile)
    _,item,r=_add_requirement(db,data,product=p,key='CUT-ORDER',required=5)
    r.board_length_mm=item.snapshot_report_length_mm=1000
    r.board_width_mm=item.snapshot_report_width_mm=200
    db.commit()
    return p,lot,profile,facts,item,r


def test_cut_pair_yield_preallocation_consume_reverse_release(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    plan=rectangular_cut_plan(db,lot,p,requirement_signature(r))
    assert plan['yield_factor']==2 and plan['utilization']==81.17
    rows=semi_finished_candidates_for_product(db,product_id=p.id,customer_id=p.customer_id,
        board_length_mm=1000,board_width_mm=200,material_code='A416D',flute_type='B',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1)
    assert rows[0].deductible_requirement_quantity==12
    assert rows[0].cut_plan==plan and not rows[0].direct_deduction_eligible
    assert rows[0].recommendation_tier == 'near'
    reverse=next(c for c in candidate_items(db,lot) if c['product_id']==p.id)
    assert reverse['cut_plan']==plan and reverse['selectable']
    kw=dict(requirement_id=r.id,requested_requirement_quantity=5,lots=[SemiFinishedLotVersion(lot.id,lot.version)],
        operator_id=data['admin'].id,idempotency_key='cut-reserve',confirmed=True,override=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING])
    result=reserve_semi_finished_inventory(db,**kw);db.commit();db.refresh(lot)
    reservation=result.reservations[0]
    assert (lot.quantity_available,lot.quantity_reserved)==(3,3)
    assert reservation.yield_factor==2 and json.loads(reservation.cut_plan_json)==plan
    assert reserve_semi_finished_inventory(db,**kw).allocated_requirement_quantity==5
    with pytest.raises(WarehouseInventoryError):
        reserve_semi_finished_inventory(db,**{**kw,'requested_requirement_quantity':4})
    assert lot.semi_finished_detail.stock_yield_per_sheet==1 and p.default_cutting_mode=='一开一'
    consume_semi_finished_reservation(db,reservation_id=reservation.id,stock_quantity=2,expected_version=lot.version,
        operator_id=data['admin'].id,idempotency_key='cut-consume')
    db.commit();db.refresh(lot)
    assert reservation.consumed_requirement_quantity==4
    reverse_semi_finished_consumption(db,reservation_id=reservation.id,stock_quantity=2,expected_version=lot.version,
        operator_id=data['admin'].id,idempotency_key='cut-reverse')
    db.commit();db.refresh(lot)
    assert reservation.consumed_requirement_quantity==0
    release_semi_finished_reservation(db,reservation_id=reservation.id,stock_quantity=3,expected_version=lot.version,
        operator_id=data['admin'].id,idempotency_key='cut-release',release_reason='取消安排')
    db.commit();db.refresh(lot)
    assert (lot.quantity_available,lot.quantity_reserved)==(6,0)
    assert reservation.released_requirement_quantity==5


def test_kerf_trim_direction_and_shaped_stock(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    facts.update(cut_trim_mm=20,cut_kerf_mm=2);profile.data_json=json.dumps(facts);db.flush()
    assert rectangular_cut_plan(db,lot,p,requirement_signature(r))['yield_factor']==1
    facts['processing']='die_cut';profile.data_json=json.dumps(facts);db.flush()
    assert rectangular_cut_plan(db,lot,p,requirement_signature(r)) is None
    candidates=candidate_items(db,lot)
    assert all(not c['selectable'] for c in candidates)
    facts['processing']='cut';facts['cut_trim_mm']=0;profile.data_json=json.dumps(facts)
    lot.semi_finished_detail.board_length_mm=440;lot.semi_finished_detail.board_width_mm=1120;db.flush()
    assert rectangular_cut_plan(db,lot,p,requirement_signature(r)) is None
    assert '异形箱' in candidate_response(db,lot)['box_styles']


def test_processed_output_never_reuses_source_board_as_rectangle(eligibility_db):
    from app.services.finished_stock_identity import product_basis
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    facts.update(scope='customers', customer_ids=[p.customer_id], product_ids=[p.id],
        processing='cut',output_piece=True, dimension_basis='source_board', physical_basis=product_basis(p))
    lot.semi_finished_detail.owner_customer_id=p.customer_id
    profile.data_json=json.dumps(facts);db.flush()
    assert rectangular_cut_plan(db,lot,p,requirement_signature(r)) is None
    rows=semi_finished_candidates_for_product(db,product_id=p.id,customer_id=p.customer_id,
        board_length_mm=1000,board_width_mm=200,material_code='A416D',flute_type='B',
        component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1)
    assert rows[0].deductible_requirement_quantity == 6
    assert rows[0].cut_plan is None
    reverse=next(c for c in candidate_items(db,lot) if c['product_id']==p.id)
    assert reverse['match_kind']=='confirmed_use' and reverse['selectable']
    facts.pop('physical_basis');profile.data_json=json.dumps(facts);db.flush()
    assert not any(c['selectable'] for c in candidate_items(db,lot))
    with pytest.raises(WarehouseInventoryError):
        reserve_semi_finished_inventory(db, requirement_id=r.id, requested_requirement_quantity=5,
            lots=[SemiFinishedLotVersion(lot.id,lot.version)], operator_id=data['admin'].id,
            idempotency_key='unknown-output',confirmed=True,override=True,
            warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING])

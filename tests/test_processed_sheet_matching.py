import json
import pytest
from sqlalchemy import select
from test_semi_finished_lot_eligibility import eligibility_db, _add_lot, _add_requirement
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import SemiFinishedLotAllowedProduct
from app.services.semi_finished_inventory import (
    semi_finished_candidates_for_product, confirm_semi_finished_match,
    ensure_semi_finished_lot_eligibility, requirement_signature,
    CUSTOMER_GENERIC_SEMI_FINISHED_STOCK, SIGNATURE_OVERRIDE_WARNING,
)
from app.services.warehouse_inventory import WarehouseInventoryError

def setup(db, data, length=790, approved=False):
    p=data['products'][0]
    lot=_add_lot(db,data,key='processed-sheet',customer_id=None,length=length)
    facts=dict(scope='customers',customer_ids=[p.customer_id],product_ids=[p.id] if approved else [],
        processing='die_cut',material_confidence='confirmed',verified_material_id=None,
        material_code='A416D',face_paper='kraft',mold_tool_id=None)
    profile=WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(facts))
    db.add(profile);db.flush()
    return p,lot,profile,facts

def candidates(db,p):
    return semi_finished_candidates_for_product(db,product_id=p.id,customer_id=p.customer_id,
        board_length_mm=800,board_width_mm=600,material_code='A416D',flute_type='B',component_type='whole',
        pieces_per_box=1,stock_yield_per_sheet=1,layer_count=3,crease_type='毛片')

def test_unknown_processed_is_visible_not_automatic_then_single_confirmation(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data)
    row=candidates(db,p)[0]
    assert not row.automatic_recommendation and not row.direct_deduction_eligible
    assert row.match_score>90
    _,_,r=_add_requirement(db,data,product=p,key='PROCESSED')
    with pytest.raises(WarehouseInventoryError,match='用途待确认'):
        confirm_semi_finished_match(db,requirement_id=r.id,inventory_lot_id=lot.id,operator_id=data['admin'].id,
            override=False,warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK])
    before=(lot.quantity_available,lot.quantity_reserved,lot.semi_finished_detail.board_length_mm)
    with pytest.raises(WarehouseInventoryError,match='用途待确认'):
        confirm_semi_finished_match(db,requirement_id=r.id,inventory_lot_id=lot.id,operator_id=data['admin'].id,
            override=True,warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING])
    assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))
    facts['product_ids']=[p.id];profile.data_json=json.dumps(facts);db.flush()
    assert candidates(db,p)[0].automatic_recommendation
    assert before==(lot.quantity_available,lot.quantity_reserved,lot.semi_finished_detail.board_length_mm)
    assert ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=p.id,customer_id=p.customer_id,
        expected=requirement_signature(r))=='customer_generic'

def test_confirmed_post_cut_identity_survives_large_dimension_reduction(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,length=400,approved=True)
    assert candidates(db,p)[0].automatic_recommendation
    assert candidates(db,p)[0].recommendation_tier == 'near'
    assert candidates(db,p)[0].direct_deduction_eligible

def test_equal_dimensions_do_not_authorize_unknown_shape(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,length=800)
    assert candidates(db,p) and not candidates(db,p)[0].direct_deduction_eligible

def test_failed_reservation_rolls_back_learned_use_and_stock(eligibility_db):
    from app.services.semi_finished_inventory import reserve_semi_finished_inventory, SemiFinishedLotVersion
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,approved=True)
    _,_,r=_add_requirement(db,data,product=p,key='ROLLBACK-PROCESSED');db.commit()
    before=(lot.quantity_available,lot.quantity_reserved,lot.version)
    with db.begin_nested() as transaction:
        reserve_semi_finished_inventory(db,requirement_id=r.id,requested_requirement_quantity=4,
            lots=[SemiFinishedLotVersion(lot.id,lot.version)],operator_id=data['admin'].id,
            idempotency_key='rollback-processed',confirmed=True,override=True,
            warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING])
        assert lot.quantity_reserved == 4
        transaction.rollback()
    db.refresh(lot)
    assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))
    assert before==(lot.quantity_available,lot.quantity_reserved,lot.version)

def test_automatic_preflight_rechecks_real_facts_and_frozen_layer(eligibility_db):
    from app.api.orders import OrderItemCreate, OrderItemReservationPlan, SemiReservationPlanEntry, _preflight_reservation_plans
    from app.services.semi_finished_inventory import direct_semi_finished_deduction_eligible
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,approved=True)
    _,_,r=_add_requirement(db,data,product=p,key='AUTO-PROCESSED')
    entry=SemiReservationPlanEntry(lot_id=lot.id,expected_version=lot.version,requested_qty=4,component_type='whole',
        recommendation_source='customer_generic',confirmed=True,direct_deduction=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK])
    payload=OrderItemCreate(product_id=p.id,quantity=5,unit_price=1,material='A416D',flute_type='B',
        reservation_plan=OrderItemReservationPlan(semi=[entry]))
    assert _preflight_reservation_plans(db,customer_id=p.customer_id,payload_items=[payload],resolved_products={1:p})
    assert not direct_semi_finished_deduction_eligible(db,lot=lot,product_id=p.id,customer_id=p.customer_id,
        expected=requirement_signature(r),layer_count=5,crease_type='毛片',crease_left_mm=None,crease_middle_mm=None,crease_right_mm=None)
    facts['product_ids']=[];profile.data_json=json.dumps(facts);db.flush()
    with pytest.raises(WarehouseInventoryError):
        _preflight_reservation_plans(db,customer_id=p.customer_id,payload_items=[payload],resolved_products={1:p})

@pytest.mark.parametrize('field,value',[('flute_type','A'),('layer_count',5),('pieces_per_box',2),('stock_yield_per_sheet',2)])
def test_physical_and_conversion_conflicts_not_automatically_relaxed(eligibility_db,field,value):
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,approved=True)
    setattr(lot.semi_finished_detail,field,value)
    if field=='layer_count': lot.semi_finished_detail.flute_type='AB'
    db.flush()
    assert not candidates(db,p)

def test_customer_exclusion_and_estimated_material_not_auto(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,approved=True)
    facts['material_confidence']='estimated';profile.data_json=json.dumps(facts);db.flush()
    assert candidates(db,p) and not candidates(db,p)[0].automatic_recommendation
    facts['customer_ids']=[data['other_customer'].id];profile.data_json=json.dumps(facts);db.flush()
    assert not candidates(db,p)

def test_raw_board_not_treated_as_post_cut(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data)
    facts['processing']='raw';profile.data_json=json.dumps(facts);lot.semi_finished_detail.sheet_type='raw_board';db.flush()
    rows=candidates(db,p)
    assert rows and not rows[0].selectable and not rows[0].automatic_recommendation
    assert '测量待核' in rows[0].match_reason  # 10mm discovery does not certify a shaped blank.

def test_blank_same_mold_multiple_codes_automatic_but_printed_not_inferred(eligibility_db):
    from app.models.mold_tool import MoldTool
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data)
    mold=MoldTool(mold_code='TEST-SHARED',mold_name='测试共刀',rack_location='测试位',is_active=True)
    db.add(mold);db.flush()
    facts.update(mold_tool_id=mold.id,mold_version=mold.version,blank_unprinted=True);profile.data_json=json.dumps(facts)
    for product in data['products'][:2]:product.mold_tool_id=mold.id
    db.flush()
    for product in data['products'][:2]:assert candidates(db,product)[0].automatic_recommendation
    facts['processing']='printed';profile.data_json=json.dumps(facts);db.flush()
    assert not candidates(db,p)[0].automatic_recommendation
    assert not candidates(db,data['other_product'])

def test_reservation_preflight_replay_and_real_quantity_path(eligibility_db):
    from app.api.orders import OrderItemCreate, OrderItemReservationPlan, SemiReservationPlanEntry, _preflight_reservation_plans
    from app.services.semi_finished_inventory import reserve_semi_finished_inventory, SemiFinishedLotVersion
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,approved=True)
    warnings=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING]
    entry=SemiReservationPlanEntry(lot_id=lot.id,expected_version=lot.version,requested_qty=4,component_type='whole',
        recommendation_source='customer_generic',confirmed=True,override=True,warning_acknowledged_codes=warnings)
    payload=OrderItemCreate(product_id=p.id,quantity=5,unit_price=1,material='A416D',flute_type='B',
        reservation_plan=OrderItemReservationPlan(semi=[entry]))
    assert _preflight_reservation_plans(db,customer_id=p.customer_id,payload_items=[payload],resolved_products={1:p})
    _,_,r=_add_requirement(db,data,product=p,key='RESERVE-PROCESSED');db.commit()
    kw=dict(requirement_id=r.id,requested_requirement_quantity=4,lots=[SemiFinishedLotVersion(lot.id,lot.version)],
        operator_id=data['admin'].id,idempotency_key='reserve-processed-once',confirmed=True,override=True,warning_acknowledged_codes=warnings)
    result=reserve_semi_finished_inventory(db,**kw);db.commit();db.refresh(lot)
    assert result.allocated_requirement_quantity==4
    assert (lot.quantity_available,lot.quantity_reserved)==(16,4)
    assert reserve_semi_finished_inventory(db,**kw).allocated_requirement_quantity==4
    assert (lot.quantity_available,lot.quantity_reserved)==(16,4)
    assert candidates(db,p)[0].automatic_recommendation

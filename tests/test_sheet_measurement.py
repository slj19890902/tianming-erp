import json
import pytest
from pydantic import ValidationError
from test_bidirectional_sheet_cut import eligibility_db, prepare
from app.services.material_candidates import candidate_items
from app.services.semi_finished_inventory import requirement_signature, ensure_semi_finished_lot_eligibility, semi_finished_candidates_for_product
from app.services.sheet_cut_plan import rectangular_cut_plan
from app.services.processed_sheet_matching import processed_match
from app.services.warehouse_inventory import WarehouseInventoryError
from app.api.warehouse_goods import SheetEntry


def test_rectangular_net_can_be_die_cut_and_measurement_is_not_stock(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    p.box_style='异形箱';p.length_mm=500;p.width_mm=100
    assert next(x for x in candidate_items(db,lot) if x['product_id']==p.id)['selectable']
    for shortage in (1,10,11):
        lot.semi_finished_detail.board_width_mm=200-shortage;db.flush()
        rows=[x for x in candidate_items(db,lot) if x['product_id']==p.id]
        # Both measured axes must be close, not an arbitrary smaller blank.
        lot.semi_finished_detail.board_length_mm=1000;db.flush()
        rows=[x for x in candidate_items(db,lot) if x['product_id']==p.id]
        assert bool(rows)==(shortage<=10)
        if rows:
            assert not rows[0]['selectable'] and rows[0]['match_kind']=='measurement_review'
        assert rectangular_cut_plan(db,lot,p,requirement_signature(r)) is None
        with pytest.raises(WarehouseInventoryError):
            ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=p.id,customer_id=p.customer_id,expected=requirement_signature(r),reviewed=True)
    lot.semi_finished_detail.board_width_mm=199
    facts['dimension_source']='label';profile.data_json=json.dumps(facts);db.flush()
    assert not [x for x in candidate_items(db,lot) if x['product_id']==p.id]


def test_tolerance_both_directions_and_hard_qualifications(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    p.box_style='异形箱';lot.semi_finished_detail.board_length_mm=1000;lot.semi_finished_detail.board_width_mm=199
    db.flush()
    rows=semi_finished_candidates_for_product(db,product_id=p.id,customer_id=p.customer_id,
        board_length_mm=1000,board_width_mm=200,material_code='A416D',flute_type='B',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1)
    assert rows and not rows[0].selectable and not rows[0].automatic_recommendation
    lot.semi_finished_detail.flute_type='E';db.flush()
    assert not processed_match(db,lot,p,requirement_signature(r))


def test_die_cut_never_recommends_liner_even_if_previously_selected(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    facts.update(processing='die_cut',product_ids=[p.id]);profile.data_json=json.dumps(facts);db.flush()
    assert not candidate_items(db,lot)
    with pytest.raises(WarehouseInventoryError):
        ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=p.id,customer_id=p.customer_id,expected=requirement_signature(r))


def test_creased_same_height_trim_and_no_die_cut(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    p.box_style='A1';p.crease_left_mm=50;p.crease_middle_mm=100;p.crease_right_mm=50
    d=lot.semi_finished_detail;d.sheet_type='creased_sheet';d.crease_type='压线'
    d.crease_left_mm=60;d.crease_middle_mm=100;d.crease_right_mm=60;d.board_width_mm=220
    facts['processing']='creased';profile.data_json=json.dumps(facts);db.flush()
    assert processed_match(db,lot,p,requirement_signature(r))['known']
    plan=rectangular_cut_plan(db,lot,p,requirement_signature(r))
    assert plan['method']=='crease_preserving_trim' and plan['yield_factor']==1
    d.crease_middle_mm=109;d.board_width_mm=229;db.flush()
    assert not processed_match(db,lot,p,requirement_signature(r))['known']
    assert rectangular_cut_plan(db,lot,p,requirement_signature(r)) is None
    d.crease_middle_mm=111;d.board_width_mm=231;db.flush()
    assert processed_match(db,lot,p,requirement_signature(r)) is None
    p.box_style='异形箱';db.flush()
    assert not [x for x in candidate_items(db,lot) if x['product_id']==p.id]


def test_crease_entry_no_free_text_and_sum_guard():
    payload=dict(facts=dict(processing='creased',dimension_source='tape'),location_id=1,expected_layout_version=1,
        quantity=10,stock_date='2026-09-16',internal_name='压线片',board_length_mm=1000,board_width_mm=610,
        layer_count=3,flute_type='B',crease_left_mm=100,crease_middle_mm=400,crease_right_mm=100,idempotency_key='measurement-entry')
    assert SheetEntry(**payload).crease_type=='压线'
    with pytest.raises(ValidationError,match='三段压线'):
        SheetEntry(**{**payload,'board_width_mm':611})
    with pytest.raises(ValidationError,match='三段压线'):
        SheetEntry(**{**payload,'facts':dict(processing='creased',dimension_source='label')})


def test_crease_trim_reserves_once_and_frozen_height_cannot_be_bypassed(eligibility_db):
    from app.services.semi_finished_inventory import reserve_semi_finished_inventory, SemiFinishedLotVersion, CUSTOMER_GENERIC_SEMI_FINISHED_STOCK, SIGNATURE_OVERRIDE_WARNING
    db,data=eligibility_db;p,lot,profile,facts,item,r=prepare(db,data)
    p.box_style='A1';p.crease_type='压线';p.crease_left_mm=50;p.crease_middle_mm=100;p.crease_right_mm=50
    item.snapshot_crease_type='压线';item.snapshot_crease_left_mm=50;item.snapshot_crease_middle_mm=100;item.snapshot_crease_right_mm=50
    d=lot.semi_finished_detail;d.sheet_type='creased_sheet';d.crease_type='压线'
    d.crease_left_mm=60;d.crease_middle_mm=100;d.crease_right_mm=60;d.board_width_mm=220
    facts['processing']='creased';profile.data_json=json.dumps(facts);db.commit()
    args=dict(requirement_id=r.id,requested_requirement_quantity=5,lots=[SemiFinishedLotVersion(lot.id,lot.version)],operator_id=data['admin'].id,
        idempotency_key='crease-trim-test',confirmed=True,override=True,warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING])
    result=reserve_semi_finished_inventory(db,**args);db.commit();db.refresh(lot)
    assert lot.quantity_reserved==5 and lot.quantity_available==1
    assert json.loads(result.reservations[0].cut_plan_json)['method']=='crease_preserving_trim'
    assert result.reservations[0].yield_factor==1
    assert reserve_semi_finished_inventory(db,**args).allocated_requirement_quantity==5
    # A different current template cannot retrospectively authorize another frozen height.
    from app.services.semi_finished_inventory import _customer_generic_crease_direction
    p.crease_middle_mm=90;p.crease_left_mm=55;p.crease_right_mm=55;db.flush()
    assert _customer_generic_crease_direction(db,r,d)=='blocked'

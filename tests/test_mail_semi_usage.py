"""Blank die-cut stock: one explicit lot/product review, never shape inference."""
import json
import pytest
from tests.test_semi_finished_order_reservation import fictional_document_evidence, b1_app, login, add_semi_lot, order_item, post_order
from sqlalchemy import select
from test_processed_sheet_matching import setup, candidates
from test_semi_finished_lot_eligibility import eligibility_db, _add_requirement
from app.models.warehouse_inventory import SemiFinishedLotAllowedProduct, InventoryReservation
from app.services.semi_finished_inventory import (
    SemiFinishedLotVersion, reserve_semi_finished_inventory,
    CUSTOMER_GENERIC_SEMI_FINISHED_STOCK, SIGNATURE_OVERRIDE_WARNING,
)
from app.services.warehouse_inventory import WarehouseInventoryError

REVIEW = 'BLANK_DIE_CUT_PRODUCT_USE_CONFIRMED'

def request(db, data, p, lot, key='review'):
    _, _, requirement = _add_requirement(db, data, product=p, key=key)
    db.commit()
    return dict(requirement_id=requirement.id, requested_requirement_quantity=4,
        lots=[SemiFinishedLotVersion(lot.id,lot.version)], operator_id=data['admin'].id,
        idempotency_key=key, confirmed=True, override=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING,REVIEW])

def test_unknown_blank_shape_can_be_reviewed_without_automatic_deduction(eligibility_db):
    db,data=eligibility_db; p,lot,profile,facts=setup(db,data,length=800)
    row=candidates(db,p)[0]
    assert row.selectable
    assert row.usage_confirmation_required
    assert not row.automatic_recommendation and not row.direct_deduction_eligible
    assert REVIEW in row.warning_codes

def test_explicit_use_reserves_once_and_learns_only_selected_lot_product(eligibility_db):
    db,data=eligibility_db; p,lot,profile,facts=setup(db,data,length=800)
    args=request(db,data,p,lot)
    before=profile.data_json
    result=reserve_semi_finished_inventory(db,**args);db.commit();db.refresh(lot)
    assert (lot.quantity_available,lot.quantity_reserved)==(16,4)
    assert REVIEW in json.loads(result.reservations[0].warning_codes)
    assert db.scalar(select(SemiFinishedLotAllowedProduct).where(
        SemiFinishedLotAllowedProduct.inventory_lot_id==lot.id,
        SemiFinishedLotAllowedProduct.product_id==p.id)).confirmed_by==data['admin'].id
    assert profile.data_json==before
    assert candidates(db,p)[0].direct_deduction_eligible
    other=candidates(db,data['products'][1])[0]
    assert other.usage_confirmation_required and not other.direct_deduction_eligible
    assert reserve_semi_finished_inventory(db,**args).allocated_requirement_quantity==4
    assert (lot.quantity_available,lot.quantity_reserved)==(16,4)

def test_missing_explicit_review_and_stale_version_cannot_learn(eligibility_db):
    db,data=eligibility_db; p,lot,profile,facts=setup(db,data,length=800)
    args=request(db,data,p,lot)
    args['warning_acknowledged_codes'].remove(REVIEW)
    with pytest.raises(WarehouseInventoryError,match='用途待确认'):
        reserve_semi_finished_inventory(db,**args)
    assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))
    args['warning_acknowledged_codes'].append(REVIEW)
    lot.version+=1;db.commit()
    with pytest.raises(WarehouseInventoryError,match='修改'):
        reserve_semi_finished_inventory(db,**args)
    assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))

@pytest.mark.parametrize('change',['printed','other_customer','excluded_product','wrong_flute','wrong_layer','multi_up','completed_part'])
def test_review_cannot_override_hard_conflicts(eligibility_db,change):
    db,data=eligibility_db; p,lot,profile,facts=setup(db,data,length=800)
    if change=='printed':facts['processing']='printed'
    if change=='other_customer':facts['customer_ids']=[data['other_customer'].id]
    if change=='excluded_product':facts['product_ids']=[data['products'][1].id]
    if change=='wrong_flute':lot.semi_finished_detail.flute_type='A'
    if change=='wrong_layer':lot.semi_finished_detail.layer_count=5;lot.semi_finished_detail.flute_type='AB'
    if change=='multi_up':lot.semi_finished_detail.stock_yield_per_sheet=2
    if change=='completed_part':facts.update(output_piece=True,dimension_basis='source_board')
    profile.data_json=json.dumps(facts);db.flush()
    args=request(db,data,p,lot)
    with pytest.raises(WarehouseInventoryError):reserve_semi_finished_inventory(db,**args)
    assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))
    assert (lot.quantity_available,lot.quantity_reserved)==(20,0)

def test_failure_rolls_back_usage_binding_and_reservation(eligibility_db,monkeypatch):
    import app.services.semi_finished_inventory as service
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,length=800)
    args=request(db,data,p,lot)
    def fail(*a,**kw):raise RuntimeError('injected movement failure')
    monkeypatch.setattr(service,'_movement',fail)
    with pytest.raises(RuntimeError,match='injected'):reserve_semi_finished_inventory(db,**args)
    db.expire_all()
    assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))
    assert not db.scalar(select(InventoryReservation.id))
    assert (lot.quantity_available,lot.quantity_reserved)==(20,0)

def test_order_preflight_requires_exact_review_and_remains_readonly(eligibility_db):
    from app.api.orders import OrderItemCreate, OrderItemReservationPlan, SemiReservationPlanEntry, _preflight_reservation_plans
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,length=800)
    entry=SemiReservationPlanEntry(lot_id=lot.id,expected_version=lot.version,requested_qty=4,
        component_type='whole',recommendation_source='customer_generic',confirmed=True,override=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING,REVIEW])
    item=OrderItemCreate(product_id=p.id,quantity=5,unit_price=1,material='A416D',flute_type='B',
        reservation_plan=OrderItemReservationPlan(semi=[entry]))
    assert _preflight_reservation_plans(db,customer_id=p.customer_id,payload_items=[item],resolved_products={1:p})
    assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))
    assert (lot.quantity_available,lot.quantity_reserved)==(20,0)
    entry.warning_acknowledged_codes.remove(REVIEW)
    with pytest.raises(WarehouseInventoryError,match='用途待确认'):
        _preflight_reservation_plans(db,customer_id=p.customer_id,payload_items=[item],resolved_products={1:p})

def test_shared_blank_can_serve_second_product_only_after_its_own_review(eligibility_db):
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,length=800)
    first=request(db,data,p,lot,'first-product')
    reserve_semi_finished_inventory(db,**first);db.commit();db.refresh(lot)
    other=data['products'][1]
    second=request(db,data,other,lot,'second-product')
    reserve_semi_finished_inventory(db,**second);db.commit();db.refresh(lot)
    assert (lot.quantity_available,lot.quantity_reserved)==(12,8)
    assert set(db.scalars(select(SemiFinishedLotAllowedProduct.product_id)))=={p.id,other.id}
    assert json.loads(profile.data_json)['product_ids']==[]

def test_recorded_21301464_dimensions_and_50_piece_demand(eligibility_db):
    """Replay only captured business facts, with fictional customer/actor identities."""
    from app.services.semi_finished_inventory import semi_finished_candidates_for_product
    db,data=eligibility_db;p,lot,profile,facts=setup(db,data,length=1345)
    p.product_code='21301464';p.box_style='异形箱'
    p.report_length_mm=1345;p.report_width_mm=1300
    p.default_material_code='R7A';p.legacy_material_text='R7A';p.mold_tool_id=None
    lot.quantity_available=126;lot.semi_finished_detail.board_width_mm=1300
    lot.semi_finished_detail.material_code_snapshot='R7A';lot.semi_finished_detail.normalized_material_code='R7A'
    facts.update(material_code='R7A',usage_confirmed=True,allow_material_substitution=True)
    profile.data_json=json.dumps(facts);db.flush()
    row=semi_finished_candidates_for_product(db,product_id=p.id,customer_id=p.customer_id,
        board_length_mm=1345,board_width_mm=1300,material_code='R7A',flute_type='B',
        component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1,layer_count=3)[0]
    assert row.selectable and row.usage_confirmation_required and not row.direct_deduction_eligible
    _,_,r=_add_requirement(db,data,product=p,key='CAPTURE-21301464')
    r.board_length_mm=1345;r.board_width_mm=1300;r.normalized_material_code='R7A'
    r.material_code_snapshot='R7A';r.required_piece_quantity=50;db.commit()
    result=reserve_semi_finished_inventory(db,requirement_id=r.id,requested_requirement_quantity=50,
        lots=[SemiFinishedLotVersion(lot.id,lot.version)],operator_id=data['admin'].id,
        idempotency_key='captured-21301464',confirmed=True,override=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING,REVIEW])
    assert result.allocated_requirement_quantity==50
    assert (lot.quantity_available,lot.quantity_reserved)==(76,50)

def test_api_preview_then_save_and_retry_reserves_shared_blank_atomically(b1_app):
    from fastapi.testclient import TestClient
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.models.warehouse_inventory import InventoryLot
    from app.models.order import Order
    from sqlalchemy import func
    app,factory=b1_app
    lot_id,version=add_semi_lot(factory,quantity=126,key='mail-blank',customer_id=None,bind_product=False)
    with factory() as db:
        db.add(WarehouseGoodsProfile(lot_id=lot_id,data_json=json.dumps(dict(
            scope='customers',customer_ids=[1],product_ids=[],material_confidence='confirmed',
            verified_material_id=None,material_code='A416D',face_paper='kraft',processing='die_cut',
            mold_tool_id=None,allow_material_substitution=True,usage_confirmed=True))))
        db.commit()
    entry=dict(lot_id=lot_id,expected_version=version,requested_qty=50,component_type='whole',
        recommendation_source='customer_generic',confirmed=True,override=True,direct_deduction=False,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,SIGNATURE_OVERRIDE_WARNING,REVIEW])
    item=order_item(1,50,dict(finished=[],semi=[entry]))
    with TestClient(app) as client:
        login(client,'admin')
        preview=client.post('/api/orders/inventory-draft-preview',json=dict(customer_id=1,items=[item]))
        assert preview.status_code==200,preview.text
        with factory() as db:
            assert not db.scalar(select(SemiFinishedLotAllowedProduct.id))
            assert db.get(InventoryLot,lot_id).quantity_available==126
        body=dict(customer_id=1,customer_po='UNIT-MAIL-BLANK',order_date='2026-10-21',
            import_integrity_status='ok',items=[item],idempotency_key='unit-mail-blank-save')
        saved=client.post('/api/orders',json=body)
        assert saved.status_code==201,saved.text
        replay=client.post('/api/orders',json=body)
        assert replay.status_code==201,replay.text
        assert replay.json()['id']==saved.json()['id']
    with factory() as db:
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved)==(76,50)
        assert db.scalar(select(func.count()).select_from(Order))==1
        assert db.scalar(select(func.count()).select_from(SemiFinishedLotAllowedProduct))==1

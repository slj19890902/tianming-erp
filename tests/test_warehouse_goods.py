import json
from datetime import date
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from pydantic import ValidationError
from test_material_candidates import prepare
from test_semi_finished_lot_product_bindings import lot_db
from test_semi_finished_lot_eligibility import eligibility_db, _add_lot, _add_requirement
from tests.test_p1_47d_inventory_adjustment import stocktake_app
from app.api.warehouse_goods import GoodsFacts, GoodsUpdate, SheetEntry, create_sheet, update_goods, get_goods
from app.api.materials import MaterialPayload, _material_write_data
from app.models.material import Material
from app.models.user import User
from app.models.audit import OperationLog
from app.models.warehouse_goods import WarehouseGoodsProfile, WarehouseGoodsMutation
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, Floor3LocationLayout
from app.services.warehouse_goods import qualification_issues
from app.services.material_candidates import candidate_items
from app.services.semi_finished_inventory import (
    ensure_semi_finished_lot_eligibility, SemiFinishedSignature, semi_finished_inventory_candidates,
    reserve_semi_finished_inventory, SemiFinishedLotVersion, CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,
)
from app.services.warehouse_inventory import WarehouseInventoryError


def material(db, code="A416D", white=False):
    row=Material(code=code, supplier_name="测试供应商", layer_count=3, is_active=True, is_white_face=white)
    db.add(row);db.flush()
    return row


def test_profile_manual_scope_replay_version_audit_and_no_stock_change(lot_db):
    db,data,lot,user=prepare(lot_db)
    m=material(db)
    facts=GoodsFacts(scope="customers",customer_ids=[data['customer'].id,data['other_product'].customer_id],
        material_confidence="confirmed",verified_material_id=m.id,processing="cut",usage_confirmed=True)
    payload=GoodsUpdate(correction_reason="测试核实用途", facts=facts,expected_version=lot.version,idempotency_key="goods-profile-save")
    before=(lot.quantity_available,lot.quantity_reserved,lot.semi_finished_detail.material_code_snapshot,
        db.scalar(select(func.count()).select_from(InventoryMovement)))
    result=update_goods(lot.id,payload,db,user)
    assert result['version']==2
    assert get_goods(lot.id,db,user)['facts']['customer_ids']==facts.customer_ids
    assert update_goods(lot.id,payload,db,user)==result
    assert db.scalar(select(func.count()).select_from(WarehouseGoodsMutation))==1
    assert before==(lot.quantity_available,lot.quantity_reserved,lot.semi_finished_detail.material_code_snapshot,
        db.scalar(select(func.count()).select_from(InventoryMovement)))
    with pytest.raises(HTTPException) as error:
        update_goods(lot.id,payload.model_copy(update={'idempotency_key':'goods-stale-save'}),db,user)
    assert error.value.status_code==409
    with pytest.raises(HTTPException):
        update_goods(lot.id,payload.model_copy(update={'expected_version':2}),db,user)


def test_profile_audit_failure_rolls_back(lot_db,monkeypatch):
    db,data,lot,user=prepare(lot_db)
    original=db.add
    def fail(row):
        if isinstance(row,OperationLog):raise RuntimeError('audit-failure')
        original(row)
    monkeypatch.setattr(db,'add',fail)
    with pytest.raises(RuntimeError):
        update_goods(lot.id,GoodsUpdate(correction_reason="测试核实用途", facts=GoodsFacts(processing="cut"),expected_version=1,idempotency_key="goods-fail-audit"),db,user)
    db.refresh(lot)
    assert lot.version==1 and db.get(WarehouseGoodsProfile,lot.id) is None


@pytest.mark.parametrize('white',[True,False])
def test_face_conflict_is_bidirectional_and_cannot_be_reserved(lot_db,white):
    db,data,lot,user=prepare(lot_db)
    stock=material(db,white=white);target=material(db,'B416D',not white)
    lot.semi_finished_detail.material_id=stock.id
    product=data['products'][0];product.material_id=target.id
    product.box_style='衬板';product.length_mm=800;product.width_mm=600
    db.commit();db.expire_all()
    item=next(i for i in candidate_items(db,lot) if i['product_id']==product.id)
    assert not item['selectable'] and '白面纸与瓦楞色不能互用' in item['warnings']
    expected=SemiFinishedSignature(customer_id=product.customer_id,board_length_mm=800,board_width_mm=600,
        normalized_material_code='A416D',flute_type='B',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1)
    with pytest.raises(WarehouseInventoryError,match='白面纸'):
        ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=product.id,customer_id=product.customer_id,expected=expected)


def test_material_entry_does_not_require_extra_confidence_approval(lot_db):
    db,data,lot,user=prepare(lot_db)
    lot.semi_finished_detail.sheet_type='raw_board'
    update_goods(lot.id,GoodsUpdate(correction_reason="测试核实用途", facts=GoodsFacts(material_confidence='estimated',estimated_material='目测牛卡'),
        expected_version=1,idempotency_key='goods-estimated'),db,user)
    data['products'][1].report_length_mm=720;db.commit()
    items=candidate_items(db,lot)
    assert items[0]['exact_dimension_match']
    assert not items[-1]['exact_dimension_match']
    assert all(i['requires_production_review'] for i in items)
    assert not any('材质为估计' in warning for warning in items[0]['warnings'])
    product=data['products'][0]
    expected=SemiFinishedSignature(customer_id=product.customer_id,board_length_mm=800,board_width_mm=600,
        normalized_material_code='A416D',flute_type='B',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1)
    assert ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=product.id,customer_id=product.customer_id,expected=expected)=='customer_generic'


def test_reverse_rejects_unitless_legacy_size_then_uses_explicit_net_mm_and_material_layer(lot_db):
    db,data,lot,user=prepare(lot_db)
    p=data['products'][0];m=material(db)
    p.material_id=m.id;p.layer_count=None;p.report_length_mm=None;p.report_width_mm=None
    p.default_cardboard_length=800;p.default_cardboard_width=600
    db.commit()
    assert p.id not in [i['product_id'] for i in candidate_items(db,lot)]
    p.box_style='衬板';p.length_mm=800;p.width_mm=600;db.commit()
    row=next(i for i in candidate_items(db,lot) if i['product_id']==p.id)
    assert row['exact_dimension_match']
    json.dumps(row)


def test_processed_goods_require_product_approval_and_multiple_customers(lot_db):
    db,data,lot,user=prepare(lot_db)
    m=material(db)
    product=data['products'][0];other=data['other_product']
    other.default_material_code="A416D"
    facts=GoodsFacts(scope='customers',customer_ids=[product.customer_id],product_ids=[product.id],
        material_confidence='confirmed',verified_material_id=m.id,processing='die_cut',usage_confirmed=True,
        allow_material_substitution=True)
    update_goods(lot.id,GoodsUpdate(correction_reason="测试核实用途", facts=facts,expected_version=1,idempotency_key='goods-die-cut'),db,user)
    assert not qualification_issues(db,lot,product)
    assert '不在已确认的适用客户范围' in qualification_issues(db,lot,other)
    facts.customer_ids.append(other.customer_id);facts.product_ids.append(other.id)
    update_goods(lot.id,GoodsUpdate(correction_reason="测试核实用途", facts=facts,expected_version=2,idempotency_key='goods-second-customer'),db,user)
    assert not qualification_issues(db,lot,other)
    assert '不在已确认的适用产品范围' in qualification_issues(db,lot,data['products'][1])


@pytest.mark.parametrize('layer,flute',[(3,'B'),(7,'AAA')])
def test_sheet_entry_typed_code_no_fake_product_replay_and_layout_cas(stocktake_app,layer,flute):
    app,factory,ids,_=stocktake_app
    with factory() as db:
        user=db.scalar(select(User).where(User.username=='p147d-admin'))
        loc=ids['loc_fg1_add'];version=db.get(Floor3LocationLayout,loc).version
        payload=SheetEntry(facts=GoodsFacts(material_code='A1B' if layer==3 else 'A1B1C1D'),location_id=loc,expected_layout_version=version,
            quantity=15,stock_date=date.today(),internal_name='通用纸板',board_length_mm=800,board_width_mm=600,
            layer_count=layer,flute_type=flute,idempotency_key=f'goods-entry-{layer}')
        # An unquoted typed code may no longer create a priceless stocktake batch.
        with pytest.raises(HTTPException,match="入库成本"):
            create_sheet(payload,db,user)
        db.add(Material(code=payload.facts.material_code,supplier_name="唯一测试供应商",layer_count=layer,
            quote_price=2,price_unit="元/㎡",purchase_currency="CNY",purchase_tax_included=True,purchase_tax_rate=0.13))
        db.commit()
        result=create_sheet(payload,db,user)
        lot=db.get(InventoryLot,result['lot_id'])
        assert lot.quantity_available==15 and lot.unit=='sheets'
        assert lot.semi_finished_detail.material_id is None
        assert lot.semi_finished_detail.material_code_snapshot==payload.facts.material_code
        assert float(lot.estimated_unit_cost_snapshot)==0.96
        assert lot.semi_finished_detail.owner_customer_id is None
        assert create_sheet(payload,db,user)==result
        count=db.scalar(select(func.count()).select_from(InventoryLot))
        db.get(Floor3LocationLayout,loc).version += 1
        db.commit()
        with pytest.raises(HTTPException) as error:create_sheet(payload.model_copy(update={'idempotency_key':'goods-entry-stale'}),db,user)
        assert error.value.status_code==409
        assert db.scalar(select(func.count()).select_from(InventoryLot))==count


def test_semi_first_actual_reservation_even_when_raw_selected_first(eligibility_db):
    db,data=eligibility_db
    product=data['products'][0]
    raw=_add_lot(db,data,key='priority-raw',customer_id=data['customer'].id)
    semi=_add_lot(db,data,key='priority-semi',customer_id=data['customer'].id)
    raw.semi_finished_detail.sheet_type='raw_board'
    m=material(db)
    for lot in [raw,semi]:
        profile=GoodsFacts(processing='raw' if lot==raw else 'cut',material_confidence='confirmed',verified_material_id=m.id,
            usage_confirmed=True,allow_material_substitution=lot==semi)
        db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=profile.model_dump_json()))
    _,_,requirement=_add_requirement(db,data,product=product,key='GOODS-PRIORITY')
    db.commit()
    candidates=semi_finished_inventory_candidates(db,requirement.id)
    assert [c.lot.id for c in candidates][:2]==[semi.id,raw.id]
    before_raw=raw.quantity_available;before_semi=semi.quantity_available
    result=reserve_semi_finished_inventory(db,requirement_id=requirement.id,requested_requirement_quantity=3,
        lots=[SemiFinishedLotVersion(raw.id,raw.version),SemiFinishedLotVersion(semi.id,semi.version)],
        operator_id=data['admin'].id,idempotency_key='goods-priority-reserve',confirmed=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK])
    assert result.allocated_requirement_quantity==3
    assert raw.quantity_available==before_raw and semi.quantity_available==before_semi-3


def test_material_omission_does_not_erase_white_flag():
    assert 'is_white_face' not in _material_write_data(MaterialPayload(code='A416D'))
    assert _material_write_data(MaterialPayload(code='A416D',is_white_face=True))['is_white_face'] is True


def test_material_white_flag_uses_versioned_history(lot_db):
    from app.services.master_data_versioning import apply_versioned_update
    db,data,lot,user=prepare(lot_db)
    m=material(db);db.commit()
    revision=apply_versioned_update(db,object_type='material',entity=m,updates={'is_white_face':True},
        expected_version=1,user=user,reason='确认白面纸',source='test.goods')
    assert revision.version==2 and m.is_white_face is True
    db.commit();db.expire_all()
    assert db.get(Material,m.id).is_white_face is True


def test_reserved_profile_and_api_permissions(lot_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.warehouse_goods import router
    from app.api.deps import get_db, get_current_user
    db,data,lot,user=prepare(lot_db)
    lot.quantity_reserved=1;db.commit()
    payload=GoodsUpdate(correction_reason="测试核实用途", facts=GoodsFacts(processing='cut'),expected_version=1,idempotency_key='goods-reserved')
    with pytest.raises(HTTPException) as error:update_goods(lot.id,payload,db,user)
    assert error.value.status_code==409
    app=FastAPI();app.include_router(router,prefix='/goods')
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[get_current_user]=lambda:user
    with TestClient(app) as client:
        assert client.get('/goods/options').status_code==200
        user.role='sales';db.commit()
        assert client.put(f'/goods/{lot.id}',json=payload.model_dump()).status_code==403


def test_migration_cannot_drop_usage_history(lot_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import importlib.util
    from pathlib import Path
    db,data,lot,user=prepare(lot_db)
    update_goods(lot.id,GoodsUpdate(correction_reason="测试核实用途", facts=GoodsFacts(processing='cut'),expected_version=1,idempotency_key='goods-history'),db,user)
    path=Path(__file__).parents[1]/'alembic/versions/rv10v8x9z70_warehouse_goods_profiles.py'
    spec=importlib.util.spec_from_file_location('goods_migration',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db.connection())):
        with pytest.raises(RuntimeError,match='history exists'):migration.downgrade()

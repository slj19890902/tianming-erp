import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from test_semi_finished_lot_product_bindings import lot_db
from app.api.material_candidates import CandidatePayload, get_candidates, save_candidates, router
from app.api.deps import get_db, get_current_user
from app.models.material_candidate import MaterialCandidateSelection
from app.models.audit import OperationLog
from app.models.warehouse_inventory import InventoryMovement, SemiFinishedLotAllowedProduct
from app.services.material_candidates import candidate_items


def prepare(lot_db):
    db, data = lot_db
    for product in data['products']:
        product.layer_count = 3
    data['customer'].chinese_short_name = '测试简称'
    db.commit()
    return db, data, data['lot'], data['admin']


def test_scoring_filters_direction_flute_scope_and_threshold(lot_db):
    db, data, lot, user = prepare(lot_db)
    a,b,c = data['products']
    b.report_length_mm = 560  # Exactly 70%, cannot select.
    c.report_length_mm = 600; c.report_width_mm = 800  # Rotation is not allowed.
    other = data['other_product']
    other.layer_count=3; other.flute_type='B'; other.report_length_mm=720; other.report_width_mm=600
    db.commit()
    items = candidate_items(db,lot)
    assert [item['score'] for item in items] == [100,90,70]
    assert items[0]['customer_name']=='测试简称'
    assert not items[-1]['selectable']
    assert '跨客户' in items[1]['warnings'][0]
    assert [item['product_id'] for item in candidate_items(db,lot,{data['customer'].id})] == [a.id,b.id]
    other.flute_type='E';db.commit()
    assert other.id not in [item['product_id'] for item in candidate_items(db,lot)]
    lot.inventory_type='finished'
    with pytest.raises(HTTPException): candidate_items(db,lot)
    db.rollback()


def test_save_reread_replay_conflict_clear_and_quantities(lot_db):
    db,data,lot,user=prepare(lot_db)
    before=(lot.quantity_available,lot.quantity_reserved,db.scalar(select(func.count()).select_from(InventoryMovement)))
    payload=CandidatePayload(product_ids=[data['products'][0].id],expected_version=lot.version,idempotency_key='candidate-save-1')
    result=save_candidates(lot.id,payload,db,user)
    assert len(result['saved'])==1
    assert get_candidates(lot.id,db,user)['saved']==result['saved']
    assert save_candidates(lot.id,payload,db,user)['version']==2
    assert db.scalar(select(func.count()).select_from(MaterialCandidateSelection))==1
    assert db.scalar(select(func.count()).select_from(SemiFinishedLotAllowedProduct))==0
    assert before==(lot.quantity_available,lot.quantity_reserved,db.scalar(select(func.count()).select_from(InventoryMovement)))
    with pytest.raises(HTTPException) as error:
        save_candidates(lot.id,payload.model_copy(update={'product_ids':[]}),db,user)
    assert error.value.status_code==409
    with pytest.raises(HTTPException) as error:
        save_candidates(lot.id,payload.model_copy(update={'idempotency_key':'candidate-save-stale'}),db,user)
    assert error.value.status_code==409
    cleared=save_candidates(lot.id,CandidatePayload(product_ids=[],expected_version=2,idempotency_key='candidate-clear'),db,user)
    assert cleared['saved']==[]


def test_reject_low_score_frozen_and_rollback_audit_failure(lot_db,monkeypatch):
    db,data,lot,user=prepare(lot_db)
    data['products'][0].report_length_mm=560;db.commit()
    payload=CandidatePayload(product_ids=[data['products'][0].id],expected_version=1,idempotency_key='candidate-fail')
    with pytest.raises(HTTPException) as error:save_candidates(lot.id,payload,db,user)
    assert error.value.status_code==422
    payload.product_ids=[data['products'][1].id]
    original_add=db.add
    def fail_audit(value):
        if isinstance(value,OperationLog):raise RuntimeError('audit failure')
        original_add(value)
    monkeypatch.setattr(db,'add',fail_audit)
    with pytest.raises(RuntimeError):save_candidates(lot.id,payload,db,user)
    db.refresh(lot)
    assert lot.version==1
    assert db.scalar(select(func.count()).select_from(MaterialCandidateSelection))==0
    monkeypatch.setattr(db,'add',original_add)
    lot.status='frozen';db.commit()
    with pytest.raises(HTTPException) as error:save_candidates(lot.id,payload,db,user)
    assert error.value.status_code==409


def test_api_permission_and_customer_scope(lot_db):
    db,data,lot,user=prepare(lot_db)
    app=FastAPI();app.include_router(router,prefix='/lots')
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[get_current_user]=lambda:user
    with TestClient(app) as client:
        assert client.get(f'/lots/{lot.id}/material-candidates').status_code==200
        user.role='sales';db.commit()
        assert client.put(f'/lots/{lot.id}/material-candidates',json={'product_ids':[], 'expected_version':1,'idempotency_key':'no-permission'}).status_code==403
        assert client.get(f'/lots/{lot.id}/material-candidates').status_code==403


def test_saved_use_remains_visible_after_product_changes(lot_db):
    db,data,lot,user=prepare(lot_db)
    product=data['products'][0]
    save_candidates(lot.id,CandidatePayload(product_ids=[product.id],expected_version=1,idempotency_key='save-before-change'),db,user)
    product.is_active=False;db.commit()
    result=get_candidates(lot.id,db,user)
    assert result['saved'][0]['product_id']==product.id
    assert product.id not in [item['product_id'] for item in result['items']]
    lot.semi_finished_detail.component_type='base';db.commit()
    assert candidate_items(db,lot)==[]
    data['products'][1].base_report_length_mm=800
    data['products'][1].base_report_width_mm=600;db.commit()
    assert candidate_items(db,lot)[0]['score']==100


def test_migration_refuses_to_delete_saved_history(lot_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import importlib.util
    from pathlib import Path
    db,data,lot,user=prepare(lot_db)
    save_candidates(lot.id,CandidatePayload(product_ids=[],expected_version=1,idempotency_key='history-guard'),db,user)
    path=Path(__file__).parents[1]/'alembic/versions/rt10v8x9z67_material_candidate_uses.py'
    spec=importlib.util.spec_from_file_location('candidate_migration',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db.connection())):
        with pytest.raises(RuntimeError,match='history exists'):migration.downgrade()

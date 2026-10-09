import hashlib
import json
import pytest
from sqlalchemy import select
from app.models.shared_finished_stock import SharedFinishedGroup,SharedFinishedPolicy
from app.services import shared_finished_stock as shared
from app.services.warehouse_inventory import WarehouseInventoryError
from tests.test_shared_finished_stock import reservation_db,setup_pair
from scripts.admin import confirm_shared_customer_codes as batch


def test_reviewed_job_no_preview_write_replay_and_outer_rollback(reservation_db,monkeypatch):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    monkeypatch.setattr(batch,'CUSTOMERS',(data['product'].customer_id,target.customer_id))
    value=batch.prepare(db);db.rollback()
    assert list(db.scalars(select(SharedFinishedGroup)))==[]
    assert len(value['groups'])==1 and value['groups'][0]['lot_ids']==[lot.id]
    with pytest.raises(WarehouseInventoryError,match='备份'):
        batch.apply_reviewed(db,expected_plan_hash=value['plan_hash'],backup_receipt={})
    db.rollback()
    receipt={'verified':True,'sha256':'1'*64}
    with pytest.raises(WarehouseInventoryError,match='固定审阅'):
        batch.apply_reviewed(db,expected_plan_hash='0'*64,backup_receipt=receipt)
    db.rollback()
    result=batch.apply_reviewed(db,expected_plan_hash=value['plan_hash'],backup_receipt=receipt)
    assert not result['replayed']
    assert db.get(SharedFinishedPolicy,result['groups'][0]['group_id']).auto_enroll
    assert batch.apply_reviewed(db,expected_plan_hash=value['plan_hash'],backup_receipt=receipt)['replayed']
    db.rollback()
    assert list(db.scalars(select(SharedFinishedGroup)))==[]


def test_printed_receipt_confirmation_is_hash_bound_narrow_and_offline(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    source=data['product']
    source.production_process=target.production_process='开槽,印刷,打钉'
    db.flush()
    frozen=json.loads(shared.product_basis(source));frozen['production_process']='开槽,打钉'
    lot.finished_detail.physical_basis_json=shared._json(frozen);db.flush()
    args=dict(product_ids=[source.id,target.id],lot_ids=[lot.id])
    with pytest.raises(WarehouseInventoryError,match='冻结规格'):shared.preview(db,**args)
    confirmation={lot.id:hashlib.sha256(shared.lot_identity(lot).encode()).hexdigest()}
    value=shared.preview(db,**args,confirmed_printed_lots=confirmation)
    with pytest.raises(WarehouseInventoryError,match='身份已变化'):
        shared.preview(db,**args,confirmed_printed_lots={lot.id:'0'*64})
    with pytest.raises(WarehouseInventoryError,match='维护任务'):
        shared._apply_confirmed(db,**args,confirmed_printed_lots=confirmation,
            preview_hash=value['preview_hash'],operation_key='unsafe-web',evidence='test',actor=data['admin'],source='web')
    frozen['material']='OTHER-MATERIAL';lot.finished_detail.physical_basis_json=shared._json(frozen);db.flush()
    confirmation={lot.id:hashlib.sha256(shared.lot_identity(lot).encode()).hexdigest()}
    with pytest.raises(WarehouseInventoryError,match='冻结规格'):
        shared.preview(db,**args,confirmed_printed_lots=confirmation)


def test_reviewed_supplement_preserves_original_missing_spec_and_rejects_automatic_or_unrelated_fields(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    frozen=json.loads(lot.finished_detail.physical_basis_json);expected=frozen['spec'];frozen['spec']=None
    original=shared._json(frozen);lot.finished_detail.physical_basis_json=original;db.flush()
    args=dict(product_ids=[data['product'].id,target.id],lot_ids=[lot.id])
    receipt={lot.id:dict(identity_sha256=hashlib.sha256(shared.lot_identity(lot).encode()).hexdigest(),fields={'spec':expected})}
    value=shared.preview(db,**args,confirmed_lot_fields=receipt)
    with pytest.raises(WarehouseInventoryError,match='维护任务'):
        shared._apply_confirmed(db,**args,confirmed_lot_fields=receipt,preview_hash=value['preview_hash'],
            operation_key='supplement-web',evidence='test',actor=data['admin'],source='web')
    with pytest.raises(WarehouseInventoryError,match='补充实物'):
        shared._preview_lots(db,[data['product'],target],[lot.id],automatic=True,confirmed_lot_fields=receipt)
    for fields in ({'material':'OTHER'},{'spec':'OTHER'},{'unit':'只'}):
        with pytest.raises(WarehouseInventoryError,match='补充实物'):
            shared.preview(db,**args,confirmed_lot_fields={lot.id:{**receipt[lot.id],'fields':fields}})
    result=shared._apply_confirmed(db,**args,confirmed_lot_fields=receipt,preview_hash=value['preview_hash'],
        operation_key='supplement-reviewed',evidence='explicit fictional physical check',actor=None,source='script')
    assert result['group_id'] and lot.finished_detail.physical_basis_json==original

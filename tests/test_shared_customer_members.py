"""Fictional departmental sharing, immutable facts and frozen reservations."""
import json
from unittest.mock import patch
import pytest
from sqlalchemy import select
from app.models.customer import Customer
from app.models.product import Product
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedMember, SharedFinishedMutation
from app.services import shared_finished_stock as shared, shared_finished_management as management
from app.services.shared_member_policy import states
from app.services.warehouse_inventory import WarehouseInventoryError, finished_inventory_candidates_for_product
from tests.test_shared_finished_stock import reservation_db, setup_pair, activate, reserve_for


def third_product(db, data):
    customer = Customer(name='虚构丙部门', customer_code='FICTION-C')
    db.add(customer); db.flush()
    source = data['product']
    fields = ('box_category','length_mm','width_mm','height_mm','flute_type','material_id',
        'report_length_mm','report_width_mm','base_report_length_mm','base_report_width_mm')
    product = Product(customer_id=customer.id,product_code=source.product_code,
        product_name='虚构丙部门产品',customer_material_code='C-MATERIAL',sale_unit_price=12,**{f:getattr(source,f) for f in fields})
    db.add(product);db.flush()
    return product


def members(db,data,gid,ids,disabled=(),key='members'):
    args=dict(group_id=gid,action='members',expected_version=db.get(SharedFinishedGroup,gid).version,
        product_ids=ids,disabled_product_ids=list(disabled))
    value=management.preview_change(db,**args)
    args.update(actor=data['admin'],preview_hash=value['preview_hash'],operation_key=key,evidence='虚构多部门实物核对')
    return management.change(db,**args),args


def test_append_third_keeps_original_inventory_prices_and_replays(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    third=third_product(db,data);db.commit()
    before=(lot.quantity_available,lot.quantity_reserved,lot.version,lot.location.id,shared.lot_identity(lot),target.sale_unit_price)
    result,args=members(db,data,gid,[data['product'].id,target.id,third.id]);db.commit()
    assert result['version']==2 and db.get(SharedFinishedMember,third.id).group_id==gid
    assert finished_inventory_candidates_for_product(db,product_id=third.id,customer_id=third.customer_id)==[lot]
    assert before==(lot.quantity_available,lot.quantity_reserved,lot.version,lot.location.id,shared.lot_identity(lot),target.sale_unit_price)
    assert management.change(db,**args)['replayed']
    assert len(list(db.scalars(select(SharedFinishedMutation))))==1


def test_pause_only_target_keeps_frozen_reservation_and_can_resume(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    reservation=reserve_for(db,data,item,lot,10,'reserve-before-pause');db.commit()
    ids=[data['product'].id,target.id]
    members(db,data,gid,ids,[target.id]);db.commit()
    assert shared.match(db,lot,product_id=target.id,customer_id=target.customer_id) is None
    assert shared.match(db,lot,product_id=data['product'].id,customer_id=data['customer'].id)
    assert shared.reserved_match(db,reservation,lot,product_id=target.id,customer_id=target.customer_id)
    assert management.detail(db,gid)['products'][1]['enabled'] is False
    members(db,data,gid,ids,key='resume');db.commit()
    assert shared.match(db,lot,product_id=target.id,customer_id=target.customer_id)
    assert (lot.quantity_available,lot.quantity_reserved)==(40,10)


def test_drift_allows_pause_but_blocks_resume_or_add(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    target.print_content='乙部门专用';target.version+=1;db.commit()
    ids=[data['product'].id,target.id]
    members(db,data,gid,ids,[target.id]);db.commit()
    with pytest.raises(WarehouseInventoryError,match='不同|变化'):
        members(db,data,gid,ids,key='bad-resume')
    db.rollback()
    assert states(db,gid)[str(target.id)] is False


def test_append_rollback_stale_and_wrong_identity(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    third=third_product(db,data);db.commit();ids=[data['product'].id,target.id,third.id]
    with patch.object(management,'append_audit_event',side_effect=RuntimeError('audit-fail')):
        with pytest.raises(RuntimeError,match='audit-fail'):members(db,data,gid,ids)
    db.rollback()
    assert db.get(SharedFinishedMember,third.id) is None and db.get(SharedFinishedGroup,gid).version==1
    args=dict(group_id=gid,action='members',expected_version=1,product_ids=ids)
    value=management.preview_change(db,**args)
    third.print_content='丙专用';third.version+=1;db.commit()
    with pytest.raises(WarehouseInventoryError,match='不同|变化'):
        management.change(db,**args,preview_hash=value['preview_hash'],operation_key='changed',evidence='核对',actor=data['admin'])
    db.rollback()
    assert db.get(SharedFinishedMember,third.id) is None


def test_cannot_drop_identity_or_cross_group_or_repeat_customer(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    with pytest.raises(WarehouseInventoryError,match='保留'):
        members(db,data,gid,[target.id])
    extra=third_product(db,data);extra.customer_id=target.customer_id;db.commit()
    with pytest.raises(WarehouseInventoryError,match='每个客户'):
        members(db,data,gid,[data['product'].id,target.id,extra.id])
    with pytest.raises(WarehouseInventoryError,match='仅活动管理员'):
        management.change(db,actor=None,group_id=gid,action='members',expected_version=1,
            product_ids=[data['product'].id,target.id],preview_hash='0'*64,operation_key='bad',evidence='bad')


def test_create_explicit_automatic_policy_is_hash_bound(reservation_db):
    from app.models.shared_finished_stock import SharedFinishedPolicy
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    args=dict(product_ids=[data['product'].id,target.id],lot_ids=[lot.id],auto_enroll=True)
    value=shared.preview(db,**args)
    with pytest.raises(WarehouseInventoryError,match='变化'):
        shared.confirm(db,**{**args,'auto_enroll':False},preview_hash=value['preview_hash'],
            operation_key='bad-auto',evidence='核实',actor=data['admin'])
    db.rollback()
    result=shared.confirm(db,**args,preview_hash=value['preview_hash'],operation_key='auto',evidence='核实',actor=data['admin']);db.commit()
    assert db.get(SharedFinishedPolicy,result['group_id']).auto_enroll is True


def test_member_update_rejects_stale_group_preview(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id'];ids=[data['product'].id,target.id]
    args=dict(group_id=gid,action='members',expected_version=1,product_ids=ids,disabled_product_ids=[target.id])
    value=management.preview_change(db,**args)
    members(db,data,gid,ids,key='newer-setting');db.commit()
    with pytest.raises(WarehouseInventoryError,match='已变化'):
        management.change(db,**args,preview_hash=value['preview_hash'],operation_key='stale-setting',evidence='核对',actor=data['admin'])
    db.rollback()
    assert states(db,gid)[str(target.id)] is True

import json
from pathlib import Path
from datetime import date
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedLot, SharedFinishedMutation, SharedFinishedPolicy, SharedFinishedOrderBasis
from app.services import shared_finished_stock as shared
from app.services import shared_finished_management as management
from app.services.warehouse_inventory import WarehouseInventoryError
from tests.test_shared_finished_stock import reservation_db, setup_pair, activate, reserve_for, add_lot


def configure(db,data,gid,*,enabled=True,automatic=True,key='configure'):
    args=dict(group_id=gid,action='configure',expected_version=db.get(SharedFinishedGroup,gid).version,
        enabled=enabled,auto_enroll=automatic)
    value=management.preview_change(db,**args)
    args.update(preview_hash=value['preview_hash'],operation_key=key,evidence='虚构现场核实',actor=data['admin'])
    return management.change(db,**args),args


def test_no_stock_creation_append_replay_and_conflicts(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    args=dict(product_ids=[data['product'].id,target.id],lot_ids=[])
    value=shared.preview(db,**args)
    result=shared.confirm(db,**args,preview_hash=value['preview_hash'],operation_key='no-stock',
        evidence='虚构产品可互换',actor=data['admin']);db.commit()
    gid=result['group_id']
    assert management.detail(db,gid)['lots'][0]['eligible']
    args=dict(group_id=gid,action='add_lots',expected_version=1,lot_ids=[lot.id])
    value=management.preview_change(db,**args)
    args.update(preview_hash=value['preview_hash'],operation_key='append',evidence='核实旧批次',actor=data['admin'])
    identity=shared.lot_identity(lot);version=lot.version
    result=management.change(db,**args);db.commit()
    assert result['version']==2 and lot.version==version+1 and shared.lot_identity(lot)==identity
    assert management.detail(db,gid)['lots'][0]['shared']
    assert management.change(db,**args)['replayed']
    with pytest.raises(WarehouseInventoryError,match='操作标识'):
        management.change(db,**{**args,'evidence':'不同核实依据'})
    db.rollback()
    assert len(list(db.scalars(select(SharedFinishedMutation))))==1


def test_policy_only_new_qualified_inbound_and_pause_does_not_reinterpret_reservation(reservation_db):
    from app.services.warehouse_inventory import finished_inventory_candidates_for_product
    db,data=reservation_db;target,item,old=setup_pair(db,data)
    gid=activate(db,data,target,old)['group_id']
    existing=add_lot(db,data,quantity=11,key='unshared-existing');db.commit()
    result,args=configure(db,data,gid);db.commit()
    assert db.get(SharedFinishedLot,existing.id) is None
    fresh=add_lot(db,data,quantity=30,key='new-auto');db.commit()
    assert db.get(SharedFinishedLot,fresh.id).group_id==gid
    assert fresh.finished_detail.owner_customer_id==data['customer'].id
    reservation=reserve_for(db,data,item,fresh,10,'reserve-new');db.commit()
    configure(db,data,gid,enabled=False,key='pause');db.commit()
    assert shared.reserved_match(db,reservation,fresh,product_id=target.id,customer_id=target.customer_id)
    assert finished_inventory_candidates_for_product(db,product_id=target.id,customer_id=target.customer_id)==[]
    later=add_lot(db,data,quantity=8,key='paused-inbound');db.commit()
    assert db.get(SharedFinishedLot,later.id) is None
    configure(db,data,gid,enabled=True,key='resume');db.commit()
    assert db.get(SharedFinishedLot,later.id) is None
    target.print_content='客户专用';target.version+=1;db.commit()
    drift=add_lot(db,data,quantity=9,key='changed-member');db.commit()
    assert db.get(SharedFinishedLot,drift.id) is None
    configure(db,data,gid,enabled=False,key='pause-drift');db.commit()
    with pytest.raises(WarehouseInventoryError,match='不同|变化'):
        configure(db,data,gid,enabled=True,key='resume-drift')


def test_management_stale_and_outer_rollback(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    _,args=configure(db,data,gid)
    db.rollback()
    assert db.get(SharedFinishedGroup,gid).version==1
    assert db.get(SharedFinishedPolicy,gid) is None
    assert list(db.scalars(select(SharedFinishedMutation)))==[]
    configure(db,data,gid,key='other-operation');db.commit()
    with pytest.raises(WarehouseInventoryError,match='已变化'):
        management.change(db,**args)
    db.rollback()
    assert db.get(SharedFinishedGroup,gid).version==2


@pytest.mark.parametrize('left,right,allowed',[
    ('粘贴,模切','模切,粘贴',True),('无需结合,模切','模切,无需结合',True),
    ('模切、粘贴','模切,粘贴',True),
    ('打钉,开槽,印刷','开槽,打钉,印刷',True),
    ('模切,冲孔','冲孔,模切',False),('模切,无需结合','模切,粘贴',False),
    ('模切,粘贴','模切,粘贴,二次粘合',False)])
def test_display_order_only_does_not_drop_real_processing(reservation_db,left,right,allowed):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    data['product'].production_process=left;target.production_process=right;db.flush()
    args=dict(product_ids=[data['product'].id,target.id],lot_ids=[])
    if allowed:
        value=shared.preview(db,**args)
        # The stored member still freezes the original process string.
        assert json.loads(value['products'][0]['identity_json'])['basis']['production_process']==left
    else:
        with pytest.raises(WarehouseInventoryError,match='工艺'):shared.preview(db,**args)


def test_auto_enrollment_audit_failure_rolls_back_whole_receipt(reservation_db,monkeypatch):
    from app.models.warehouse_inventory import InventoryLot
    db,data=reservation_db;target,item,old=setup_pair(db,data)
    gid=activate(db,data,target,old)['group_id'];configure(db,data,gid);db.commit()
    before=list(db.scalars(select(InventoryLot.id)))
    def fail(*a,**kw):raise RuntimeError('fictional audit failure')
    monkeypatch.setattr(management,'append_audit_event',fail)
    with pytest.raises(RuntimeError,match='audit failure'):add_lot(db,data,quantity=8,key='rollback-receipt')
    db.rollback()
    assert list(db.scalars(select(InventoryLot.id)))==before


@pytest.mark.parametrize('change',[None,'missing_basis','changed_order','changed_printing'])
def test_qualified_production_surplus_keeps_original_order_reservation(reservation_db,change):
    from app.models.production import ProductionTask
    from app.models.order import OrderItem
    from app.services.shared_finished_receipts import PAIRS
    from app.services.production_workflow import _new_task_printing_snapshot
    db,data=reservation_db;target,item,old=setup_pair(db,data)
    gid=activate(db,data,target,old)['group_id'];configure(db,data,gid);db.commit()
    product=data['product'];basis=json.loads(shared.product_basis(product))
    owner_item=OrderItem(order_id=data['order'].id,product_id=product.id,quantity=50,
        unit_price=1,subtotal=50,material_status='pending',requisition_status='未报料',
        snapshot_product_code=product.product_code,snapshot_product_name=product.product_name,
        snapshot_spec=basis['spec'],supply_mode_snapshot='corrugated_production',special_process='无',
        combination_role='standalone')
    for field,key in PAIRS.items():setattr(owner_item,field,basis.get(key))
    from app.services.box_type_rules import order_snapshot_box_configuration
    configuration=order_snapshot_box_configuration(product)
    for key in ("splice_mode","pieces_per_box","flap_mm"):setattr(owner_item,"snapshot_"+key,configuration[key])
    owner_item.special_process=configuration["default_cutting_mode"]
    for field in ('report_length_mm','report_width_mm','base_report_length_mm','base_report_width_mm'):
        setattr(owner_item,'snapshot_'+field,getattr(product,field))
    owner_item.sheet_cutting_settings_snapshot=product.sheet_cutting_settings
    db.add(owner_item);db.flush()
    assert db.get(SharedFinishedOrderBasis,owner_item.id) is not None
    task=ProductionTask(order_item_id=owner_item.id,status='pending',planned_quantity=50,
        **_new_task_printing_snapshot(db,product))
    db.add(task);db.flush()
    from app.services.warehouse_inventory import manual_finished_in
    # The specialized production integration runs after the original order's
    # reservation, so that only the free balance becomes visible to the peer.
    lot=manual_finished_in(db,customer_id=product.customer_id,product_id=product.id,
        location_id=data['location'].id,quantity=30,stock_date=date.today(),
        source_type='production_surplus',source_ref_type='production_completion',source_ref_id=9876,
        remarks=None,operator_id=data['admin'].id,idempotency_key='production-surplus')
    assert db.get(SharedFinishedLot,lot.id) is None
    reserve_for(db,data,owner_item,lot,10,'own-production-reserve')
    completion=SimpleNamespace(id=9876,task_id=task.id,order_item_id=owner_item.id)
    if change=='missing_basis':db.delete(db.get(SharedFinishedOrderBasis,owner_item.id));db.flush()
    if change=='changed_order':owner_item.snapshot_spec='DIFFERENT PHYSICAL SPEC';db.flush()
    if change=='changed_printing':task.print_content_snapshot='CUSTOMER-ONLY-PRINT';db.flush()
    assert management.enroll_production_receipt(db,lot,completion=completion,item=owner_item,operator_id=data['admin'].id)==(change is None)
    db.commit()
    assert (lot.quantity_available,lot.quantity_reserved)==(20,10)
    assert bool(shared.match(db,lot,product_id=target.id,customer_id=target.customer_id))==(change is None)


def test_policy_migration_roundtrip_and_nonempty_downgrade_refused(reservation_db):
    import importlib.util
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    from desktop_assistant.migration import facts
    db,data=reservation_db;target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    engine=db.get_bind()
    for model in (SharedFinishedOrderBasis,SharedFinishedMutation,SharedFinishedPolicy):model.__table__.drop(engine)
    path=Path(engine.url.database);before=facts(path)
    spec=importlib.util.spec_from_file_location('management_migration',Path(__file__).parents[1]/'alembic/versions/ej1009sm_shared_finished_management.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with engine.begin() as con:
        with Operations.context(MigrationContext.configure(con)):
            module.upgrade();module.downgrade();module.upgrade()
        assert con.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
    assert facts(path,before['columns'])==before
    configure(db,data,gid);db.commit()
    with engine.begin() as con:
        with Operations.context(MigrationContext.configure(con)):
            with pytest.raises(RuntimeError,match='禁止有损降级'):module.downgrade()
        with pytest.raises(Exception,match='immutable'):
            con.exec_driver_sql('DELETE FROM shared_finished_mutations')

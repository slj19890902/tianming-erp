from datetime import date, datetime
from decimal import Decimal
import json

import pytest
from sqlalchemy import select, func
from tests.test_inventory_cost_snapshot import db, _material
from app.services.composite_bom import replace_product_bom
from app.models.customer import Customer
from app.models.user import User
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, WarehouseLocation, InventoryLotTransfer, InventoryReservation
from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
from app.models.audit import OperationLog
from app.models.finance import FinanceIdempotencyRecord
from app.services.finished_stock_identity import product_basis
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services import emergency_kit_correction_20261014 as service
from app.services.warehouse_inventory import WarehouseInventoryError


@pytest.fixture
def kit(db):
    db.add(Customer(id=136,customer_number=136,customer_code='KIT-TEST-136',name='隔离用户'))
    actor=User(username='kit-correction-admin',password_hash='unusable',role='admin',is_active=True,real_name='测试管理员')
    db.add(actor);db.flush()
    material=_material(db,code='VSNIV',price='2.95');material.layer_count=5
    common=dict(customer_id=136,product_code='Z.001.000120',customer_material_code='Z.001.000120',
        box_category='normal',material_id=material.id,flute_type='AB',layer_count=5,pieces_per_box=1,
        splice_mode='single',unit='只',production_process='无需结合',crease_type='净料',default_cutting_mode='一开一')
    lower=Product(id=3496,product_name='独立底',box_style='独立底',length_mm=450,width_mm=450,height_mm=210,
        report_length_mm=890,report_width_mm=890,**common)
    upper=Product(id=3787,product_name='上盖',box_style='独立天盖',length_mm=470,width_mm=470,height_mm=70,
        report_length_mm=640,report_width_mm=640,**common)
    parent=Product(id=3863,customer_id=136,product_code='Z.001.000120',customer_material_code='Z.001.000120',
        product_name='组套父件',unit='套',box_style='BOM组合',pieces_per_box=1,splice_mode='single',production_process='无需结合',
        composite_fulfillment_mode='parent_delivery')
    db.add_all([lower,upper,parent]);db.flush()
    replace_product_bom(db,parent_product_id=3863,inventory_mode='assembled',material_mode='expand_children',delivery_mode='parent',
        components=[{'component_product_id':pid,'quantity_per_set':1,'inventory_relation':'assembly'} for pid in (3496,3787)],
        expected_version=parent.version,user=actor)
    location=WarehouseLocation(id=1918,location_code='TEST-KIT-LOCATION',location_name='原位',warehouse_type='finished',
        warehouse_floor=1,placement_status='placed',is_active=True,source_version='P1-25C',storage_type='temporary_aisle',area_code='DISPATCH')
    order=Order(id=9782,order_number='TEST-10552-KIT',customer_id=136,order_date=date.today(),status='pending_production')
    db.add_all([location,order]);db.flush()
    item=OrderItem(id=10552,order_id=order.id,product_id=3863,quantity=50,unit_price=Decimal('14.58'),subtotal=Decimal('729'),
        snapshot_product_name='组套父件',snapshot_product_code='Z.001.000120',composite_fulfillment_mode_snapshot='parent_delivery')
    db.add(item);db.flush();freeze_master_order_bom(db,order_item_id=item.id,actor=actor)
    for lid,product,cost in [(666,lower,'11.4627'),(1421,upper,'1.3640')]:
        lot=InventoryLot(id=lid,lot_number='TEST-LOT-'+str(lid),inventory_type='finished',warehouse_location_id=1918,
            quantity_available=210,unit='boxes',status='active',source_type='stocktake',stock_date=date.today(),last_movement_at=datetime.now(),
            estimated_unit_cost_snapshot=Decimal(cost),cost_snapshot_source='inventory_confirmed_material',
            cost_snapshot_detail_json=json.dumps({'currency':'CNY','estimate_basis':'approved_stocktake_cost_rule'}))
        lot.finished_detail=FinishedGoodsInventoryDetail(owner_customer_id=136,product_id=product.id,is_general=False,
            inventory_code_snapshot=product.product_code,product_name_snapshot=product.product_name,box_type_snapshot=product.box_style,
            length_mm=product.length_mm,width_mm=product.width_mm,height_mm=product.height_mm,material_code_snapshot='VSNIV',flute_type_snapshot='AB',
            physical_basis_json=None if lid==666 else product_basis(product))
        db.add(lot)
    order.status="cancelled"
    db.commit()
    return db,actor


def execute(db,actor,**changes):
    return service.execute(db,actor=actor,preview_hash=service.preview(db,actor=actor)['preview_hash'],
        operation_key='reviewed-50-sets',evidence='用户明确从原上下盖各取50组成50套',physical_match_confirmed=True,**changes)


def test_only_child50_confirmed_assembly_and_original160_history_kept(kit):
    db,actor=kit
    before=service.preview(db,actor=actor)
    assert before['total_cost']=='641.3350'
    assert db.scalar(select(func.count(InventoryLot.id)))==2
    result=execute(db,actor);assert result['quantity']==50
    assert result['reference_total_cost']=='641.3350'
    assert result['reference_unit_cost']=='12.8267'
    lower=db.get(InventoryLot,666);upper=db.get(InventoryLot,1421);child=db.get(InventoryLot,result['split_lot_id'])
    assert lower.quantity_available==upper.quantity_available==160
    assert lower.finished_detail.physical_basis_json is None
    assert child.quantity_available==0 and child.quantity_consumed==50
    assert child.finished_detail.physical_basis_json==before['bases'][3496]
    assert upper.quantity_consumed==50
    edge=db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id==child.id))
    assert edge.source_lot_id==666 and edge.source_location_id==edge.target_location_id==1918
    assert edge.source_version_before==1 and edge.quantity==50
    proof=json.loads(db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.action==service.ACTION)).response_json)['contract']
    assert {(r['lot_id'],r['quantity']) for r in proof['inputs']}=={(child.id,50),(1421,50)}
    assert db.scalar(select(func.count(BomAssembly.id)))==0
    assert db.get(Order,9782).status=='cancelled'
    assert db.get(InventoryLot,result['output_lot_id']).quantity_available==50
    assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.action_code==service.ACTION))==1
    db.rollback()
    assert db.scalar(select(func.count(InventoryLot.id)))==2
    assert db.get(InventoryLot,666).quantity_available==210
    assert db.get(InventoryLot,1421).quantity_available==210


def test_same_key_replay_and_other_actor_or_payload_rejected(kit):
    db,actor=kit;p=service.preview(db,actor=actor)
    kwargs=dict(actor=actor,preview_hash=p['preview_hash'],operation_key='fixed-operation',evidence='确认50套',physical_match_confirmed=True)
    first=service.execute(db,**kwargs)
    record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.action==service.ACTION))
    assert json.loads(record.response_json)['receipt']==first
    # A bounded or legacy audit envelope cannot become the replay source.
    log=db.scalar(select(OperationLog).where(OperationLog.action_code==service.ACTION))
    log.details=json.dumps({'finance_receipt_id':record.id});db.flush()
    again=service.execute(db,**kwargs);assert again['replayed'] and again['output_lot_id']==first['output_lot_id']
    with pytest.raises(WarehouseInventoryError,match='标识'):service.execute(db,**{**kwargs,'evidence':'其他确认'})
    assert db.scalar(select(func.count(BomAssembly.id)))==0
    other=User(username='other-admin',password_hash='unusable',role='admin',is_active=True,real_name='其他测试管理员');db.add(other);db.flush()
    with pytest.raises(WarehouseInventoryError,match='标识'):service.execute(db,**{**kwargs,'actor':other})


@pytest.mark.parametrize('change',[{'quantity':49},{'quantity':51},{'quantity':True},{'product_id':3496},{'target_location_id':1}])
def test_fixed_fifty_scope_rejected_without_writes(kit,change):
    db,actor=kit
    with pytest.raises(WarehouseInventoryError):service.preview(db,actor=actor,**change)
    assert db.scalar(select(func.count(InventoryLot.id)))==2


@pytest.mark.parametrize('change',['version','master','cost','quantity','basis'])
def test_stale_preview_product_or_source_change_rejected(kit,change):
    db,actor=kit;p=service.preview(db,actor=actor)
    if change=='version':db.get(InventoryLot,1421).version+=1
    elif change=='master':db.get(Product,3496).production_process='印刷'
    elif change=='cost':db.get(InventoryLot,1421).estimated_unit_cost_snapshot=Decimal('9')
    elif change=='quantity':db.get(InventoryLot,1421).quantity_available=209
    elif change=='basis':db.get(InventoryLot,666).finished_detail.physical_basis_json=product_basis(db.get(Product,3496))
    db.commit()
    with pytest.raises(WarehouseInventoryError):service.execute(db,actor=actor,preview_hash=p['preview_hash'],operation_key='stale',evidence='确认',physical_match_confirmed=True)
    assert db.scalar(select(func.count(InventoryLot.id)))==2


@pytest.mark.parametrize('stage',['after_assembly','audit'])
def test_unexpected_failure_rolls_back_child_basis_debits_and_output(kit,monkeypatch,stage):
    db,actor=kit
    if stage=='after_assembly':
        original=service.inv.manual_finished_in
        def fail(*args,**kw):
            original(*args,**kw);raise RuntimeError('kit injected')
        monkeypatch.setattr(service.inv,'manual_finished_in',fail)
    else:
        def fail(*args,**kw):raise RuntimeError('kit injected')
        monkeypatch.setattr(service,'append_audit_event',fail)
    with pytest.raises(RuntimeError,match='injected'):execute(db,actor)
    db.expire_all()
    assert db.get(InventoryLot,666).quantity_available==db.get(InventoryLot,1421).quantity_available==210
    assert db.get(InventoryLot,666).finished_detail.physical_basis_json is None
    assert db.scalar(select(func.count(InventoryLot.id)))==2
    assert db.scalar(select(func.count(BomAssembly.id)))==0
    assert db.scalar(select(func.count(InventoryLotTransfer.id)))==0
    assert db.scalar(select(func.count(FinanceIdempotencyRecord.id)))==0


@pytest.mark.parametrize('field',['quantity_reserved','quantity_consumed','quantity_damaged','quantity_scrapped'])
def test_active_or_abnormal_lot_rejected(kit,field):
    db,actor=kit;setattr(db.get(InventoryLot,666),field,1);db.commit()
    with pytest.raises(WarehouseInventoryError):service.preview(db,actor=actor)


def test_other_live_reservation_and_admin_permissions(kit):
    db,actor=kit
    r=InventoryReservation(reservation_number='BAD-LIVE',inventory_lot_id=666,reservation_type='finished_order',
        order_id=9782,order_item_id=10552,reserved_stock_quantity=1,credited_requirement_quantity=1,status='active')
    db.add(r);db.commit()
    with pytest.raises(WarehouseInventoryError,match='预占'):service.preview(db,actor=actor)
    r.status='cancelled';db.commit();actor.role='boss'
    with pytest.raises(WarehouseInventoryError) as exc:service.preview(db,actor=actor)
    assert exc.value.status_code==403


@pytest.mark.parametrize('state',['cancelled','closed','dead','completed','archived','delivered'])
def test_physical_contract_never_reopens_cancelled_or_finished_reference_order(kit,state):
    db,actor=kit;db.get(Order,9782).status=state;db.commit()
    receipt=execute(db,actor)
    assert receipt['quantity']==50
    assert db.get(Order,9782).status==state
    assert db.scalar(select(func.count(InventoryLot.id)))==4


def test_corrupt_full_receipt_cannot_duplicate_assembly(kit):
    db,actor=kit;p=service.preview(db,actor=actor)
    args=dict(actor=actor,preview_hash=p['preview_hash'],operation_key='corrupt-test',evidence='确认50套',physical_match_confirmed=True)
    service.execute(db,**args)
    row=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.action==service.ACTION))
    row.response_json='{}';db.flush()
    with pytest.raises(WarehouseInventoryError,match='回执损坏'):service.execute(db,**args)
    assert db.scalar(select(func.count(BomAssembly.id)))==0


def test_wrong_source_customer_never_confirms_identity(kit):
    db,actor=kit;db.add(Customer(id=137,customer_number=137,customer_code='OTHER',name='其他客户'));db.flush()
    db.get(InventoryLot,666).finished_detail.owner_customer_id=137;db.commit()
    with pytest.raises(WarehouseInventoryError):service.preview(db,actor=actor)
    assert db.get(InventoryLot,666).finished_detail.physical_basis_json is None



def _source_output(db, actor):
    receipt=execute(db,actor)
    return receipt,db.get(InventoryLot,receipt['output_lot_id'])


def test_cost_frozen_across_future_master_and_legal_split_and_move(kit):
    from app.services.inventory_valuation import frozen_cost
    db,actor=kit;receipt,output=_source_output(db,actor)
    assert frozen_cost(output,db)[0]==Decimal('12.8267')
    parent=db.get(Product,3863);parent.production_process='后来改为印刷';parent.version+=1;db.flush()
    assert frozen_cost(output,db)[0]==Decimal('12.8267')
    child=service.split_for_correction(db,output,20,actor,'physical-output-split-20')
    assert frozen_cost(child,db)[0]==Decimal('12.8267')
    assert frozen_cost(output,db)[0]==Decimal('12.8267')
    target=WarehouseLocation(id=1919,location_code='TEST-KIT-MOVE',location_name='派生移库',warehouse_type='finished',
        warehouse_floor=1,placement_status='placed',is_active=True,source_version='P1-25C',storage_type='temporary_aisle',area_code='DISPATCH')
    db.add(target);db.flush()
    moved=service.inv.transfer_finished_lot_between_locations(db,lot_id=child.id,expected_version=child.version,
        quantity=10,location_id=target.id,operator_id=actor.id,idempotency_key='physical-output-move-10')
    lot=db.get(InventoryLot,moved.target_lot.id)
    assert frozen_cost(lot,db)[0]==Decimal('12.8267')
    assert frozen_cost(child,db)[0]==Decimal('12.8267')
    assert lot.warehouse_location_id==1919


@pytest.mark.parametrize('fault',['consume','hash','fake_source','cycle','missing_edge','child_basis','excess_quantity'])
def test_cost_rejects_corrupt_finance_or_typed_lineage(kit,fault):
    from app.services.inventory_valuation import frozen_cost
    from app.models.warehouse_inventory import InventoryMovement
    db,actor=kit;receipt,output=_source_output(db,actor)
    record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.action==service.SOURCE))
    proof=json.loads(record.response_json)
    if fault=='consume':db.get(InventoryMovement,proof['contract']['inputs'][0]['movement_id']).quantity=49
    elif fault=='hash':record.request_hash='0'*64
    elif fault=='child_basis':db.get(InventoryLot,receipt['split_lot_id']).finished_detail.physical_basis_json='{}'
    else:
        child=service.split_for_correction(db,output,20,actor,'physical-output-corruption-child')
        edge=db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id==child.id))
        if fault=='fake_source':edge.source_lot_id=666
        elif fault=='cycle':edge.target_lot_id=output.id;edge.source_lot_id=child.id
        elif fault=='missing_edge':db.delete(edge)
        elif fault=='excess_quantity':child.quantity_available+=1
        output=child
    db.flush();cost,basis=frozen_cost(output,db)
    assert cost is None and basis.get('validation_issue')


def _unordered_test_delivery(db,actor,output):
    from app.models.delivery import Delivery,DeliveryItem
    from app.models.warehouse_inventory import UnorderedFinishedDeliveryAllocation
    from app.services.delivery_snapshots import sales_contract
    from app.services.delivery_quantities import product_basis as quantity_basis,encode
    delivery=Delivery(delivery_number='ISOLATED-PHYSICAL-KIT-DISPATCH',customer_id=136,
        delivery_date=date.today(),source_mode='unordered_finished',status='pending',total_quantity=50,created_by=actor.id)
    db.add(delivery);db.flush()
    line=DeliveryItem(delivery_id=delivery.id,product_id=3863,order_item_id=None,source_type='unordered_finished',
        delivered_quantity=50,product_code_snapshot='Z.001.000120',product_name_snapshot='组套父件',unit_snapshot='套',
        unit_price_snapshot=Decimal('14.58'),price_source='manual',
        quantity_contract_json=encode(quantity_basis(db.get(Product,3863)),50),
        sales_contract_json=sales_contract(unit='套',price='14.58',tax_mode='tax_inclusive',tax_rate='0.13',source={'kind':'isolated_test'}))
    db.add(line);db.flush()
    allocation=UnorderedFinishedDeliveryAllocation(delivery_item_id=line.id,inventory_lot_id=output.id,
        planned_quantity=50,status='planned',lot_number_snapshot=output.lot_number,warehouse_location_id_snapshot=1918,created_by=actor.id)
    db.add(allocation);db.flush()
    return delivery,line,allocation


def test_normal_unordered_dispatch_cancel_restores_parent_only_and_repeats_safely(kit):
    from app.services.unordered_finished_delivery import dispatch_unordered_finished_inventory,cancel_unordered_finished_dispatch
    from app.services.inventory_valuation import frozen_cost
    db,actor=kit;receipt,output=_source_output(db,actor)
    delivery,line,allocation=_unordered_test_delivery(db,actor,output)
    dispatch_unordered_finished_inventory(db,delivery=delivery,delivery_items=[line],operator_id=actor.id,dispatched_at=datetime.now())
    assert output.quantity_available==0 and output.quantity_consumed==50
    assert allocation.consumed_quantity==50
    assert frozen_cost(output,db)[0]==Decimal('12.8267')
    cancel_unordered_finished_dispatch(db,delivery=delivery,delivery_items=[line],operator_id=actor.id)
    db.flush();db.expire_all();output=db.get(InventoryLot,output.id)
    assert output.quantity_available==50 and output.quantity_consumed==0
    assert allocation.restored_quantity==50
    cancel_unordered_finished_dispatch(db,delivery=delivery,delivery_items=[line],operator_id=actor.id)
    assert output.quantity_available==50
    assert frozen_cost(output,db)[0]==Decimal('12.8267')
    assert db.get(InventoryLot,666).quantity_available==160 and db.get(InventoryLot,666).finished_detail.physical_basis_json is None
    assert db.get(InventoryLot,1421).quantity_available==160 and db.get(InventoryLot,1421).quantity_consumed==50
    assert db.get(InventoryLot,receipt['split_lot_id']).quantity_consumed==50
    assert db.get(Order,9782).status=='cancelled'



def test_corrupt_contract_replay_fails_without_duplicate_consumption(kit):
    db,actor=kit;proposal=service.preview(db,actor=actor)
    args=dict(actor=actor,preview_hash=proposal['preview_hash'],operation_key='full-contract-replay',evidence='确认50套',physical_match_confirmed=True)
    receipt=service.execute(db,**args)
    record=db.get(FinanceIdempotencyRecord,receipt['source_record_id'])
    value=json.loads(record.response_json);value['contract']['total_cost']='1.00'
    record.response_json=json.dumps(value);db.flush()
    with pytest.raises(WarehouseInventoryError,match='回执损坏'):service.execute(db,**args)
    assert db.get(InventoryLot,1421).quantity_available==160
    assert db.get(InventoryLot,receipt['split_lot_id']).quantity_consumed==50
    assert db.scalar(select(func.count(InventoryLot.id)))==4



def test_real_entry_guard_session_commit_validates_and_posts_source(kit):
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    from app.services.inventory_valuation import frozen_cost
    db,actor=kit;actor_id=actor.id
    with ReceiptPriceGuardSession(bind=db.get_bind(),expire_on_commit=False) as guarded:
        receipt=execute(guarded,guarded.get(User,actor_id))
        assert receipt['output_lot_id'] in guarded.info['new_inventory_entry_ids']
        guarded.commit()
        assert frozen_cost(guarded.get(InventoryLot,receipt['output_lot_id']),guarded)[0]==Decimal('12.8267')
        assert guarded.get(Order,9782).status=='cancelled'
    db.expire_all()
    assert db.get(InventoryLot,receipt['output_lot_id']).quantity_available==50


@pytest.mark.parametrize('fault',['forged_receipt','missing_child_spec'])
def test_guard_commit_rejects_forged_or_incomplete_source_and_rolls_back(kit,fault):
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    from fastapi import HTTPException
    db,actor=kit;actor_id=actor.id
    with ReceiptPriceGuardSession(bind=db.get_bind(),expire_on_commit=False) as guarded:
        receipt=execute(guarded,guarded.get(User,actor_id))
        record=guarded.get(FinanceIdempotencyRecord,receipt['source_record_id'])
        proof=json.loads(record.response_json)
        if fault=='forged_receipt':record.request_hash='f'*64
        else:
            part=proof['contract']['inputs'][0]
            child=json.loads(part['basis']);child['spec']=None;part['basis']=json.dumps(child)
            record.response_json=json.dumps(proof)
        with pytest.raises(HTTPException) as error:guarded.commit()
        assert error.value.status_code==422
        guarded.rollback()
    db.expire_all()
    assert db.get(InventoryLot,666).quantity_available==db.get(InventoryLot,1421).quantity_available==210
    assert db.get(InventoryLot,666).finished_detail.physical_basis_json is None
    assert db.scalar(select(func.count(InventoryLot.id)))==2
    assert db.scalar(select(func.count(FinanceIdempotencyRecord.id)))==0


def test_frozen_finished_specs_guard_is_exact_and_cannot_accept_missing_children(kit):
    from app.core.inventory_entry_guard import _physical_kit_specifications
    from app.services.inventory_valuation import frozen_cost
    db,actor=kit;receipt,lot=_source_output(db,actor)
    _,detail=frozen_cost(lot,db);basis=json.loads(lot.finished_detail.physical_basis_json)
    assert _physical_kit_specifications(detail,basis)
    copied=json.loads(json.dumps(detail));copied['frozen_assembly_spec'].pop()
    assert not _physical_kit_specifications(copied,basis)
    copied=json.loads(json.dumps(detail));copied['frozen_assembly_spec'][0]['physical_basis']['spec']=''
    assert not _physical_kit_specifications(copied,basis)
    copied=json.loads(json.dumps(detail));copied['frozen_assembly_spec'][0]['quantity_per_set']=2
    assert not _physical_kit_specifications(copied,basis)


@pytest.mark.parametrize('fault',['typed_quantity','source_missing','target_missing','source_balance','target_balance','wrong_lot','version','location','key'])
def test_descendant_typed_edge_must_match_real_transfer_movements(kit,fault):
    from app.models.warehouse_inventory import InventoryMovement
    from app.services.inventory_valuation import frozen_cost
    db,actor=kit;receipt,output=_source_output(db,actor)
    child=service.split_for_correction(db,output,20,actor,'review-typed-evidence')
    edge=db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id==child.id))
    source=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==service.inv._transfer_key('goods-correction','review-typed-evidence','source')))
    target=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==service.inv._transfer_key('goods-correction','review-typed-evidence','target')))
    assert source.quantity==target.quantity==edge.quantity==20
    assert frozen_cost(child,db)[0]==Decimal('12.8267')
    if fault=='typed_quantity':edge.quantity=edge.available_quantity=19
    elif fault=='source_missing':db.delete(source)
    elif fault=='target_missing':db.delete(target)
    elif fault=='source_balance':source.before_available-=1
    elif fault=='target_balance':target.after_available-=1
    elif fault=='wrong_lot':target.inventory_lot_id=666
    elif fault=='version':edge.source_version_before+=1;edge.source_version_after+=1
    elif fault=='location':
        db.add(WarehouseLocation(id=1919,location_code='TEST-DAMAGED-EDGE',location_name='错误位置',warehouse_type='finished'))
        db.flush();edge.target_location_id=1919
    elif fault=='key':edge.idempotency_key='correction:other-unrelated-operation'
    db.flush()
    for node in (child,output):
        cost,detail=frozen_cost(node,db)
        assert cost is None and detail.get('validation_issue')


@pytest.mark.parametrize('fault',[None,'quantity','missing_target','self_balance','self_components'])
def test_normal_partial_and_full_self_move_are_bound_to_actual_ledgers(kit,fault):
    from app.models.warehouse_inventory import InventoryMovement
    from app.services.inventory_valuation import frozen_cost
    db,actor=kit;receipt,output=_source_output(db,actor)
    # A legitimate metadata version change is not inferred from ledger count.
    output.version+=1;db.flush()
    target=WarehouseLocation(id=1919,location_code='TEST-MOVE-LEDGER',location_name='移库',warehouse_type='finished',
        warehouse_floor=1,placement_status='placed',is_active=True,source_version='P1-25C',storage_type='temporary_aisle',area_code='DISPATCH')
    db.add(target);db.flush()
    moved=service.inv.transfer_finished_lot_between_locations(db,lot_id=output.id,expected_version=output.version,
        quantity=20,location_id=1919,operator_id=actor.id,idempotency_key='correction:normal-parent-partial-20')
    child=moved.target_lot
    whole=service.inv.transfer_finished_lot_between_locations(db,lot_id=child.id,expected_version=child.version,
        quantity=20,location_id=1918,operator_id=actor.id,idempotency_key='normal-parent-full-20')
    assert whole.target_lot.id==child.id
    assert frozen_cost(child,db)[0]==Decimal('12.8267')
    if fault=='quantity':moved.transfer.quantity=moved.transfer.available_quantity=19
    elif fault=='missing_target':
        row=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==service.inv._transfer_key('location-transfer','correction:normal-parent-partial-20','target')))
        db.delete(row)
    elif fault=='self_components':
        whole.transfer.available_quantity=19;whole.transfer.reserved_quantity=1
    elif fault=='self_balance':
        row=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==service.inv._transfer_key('location-transfer','normal-parent-full-20','source')))
        row.after_available-=1
    db.flush()
    for node in (output,child):
        cost,detail=frozen_cost(node,db)
        if fault:assert cost is None and detail.get('validation_issue')
        else:assert cost==Decimal('12.8267')

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
    for lid,product,cost in [(666,lower,'11.4627'),(1421,upper,'3.1173')]:
        lot=InventoryLot(id=lid,lot_number='TEST-LOT-'+str(lid),inventory_type='finished',warehouse_location_id=1918,
            quantity_available=210,unit='boxes',status='active',source_type='stocktake',stock_date=date.today(),last_movement_at=datetime.now(),
            estimated_unit_cost_snapshot=Decimal(cost),cost_snapshot_source='inventory_confirmed_material',
            cost_snapshot_detail_json=json.dumps({'currency':'CNY','estimate_basis':'approved_stocktake_cost_rule'}))
        lot.finished_detail=FinishedGoodsInventoryDetail(owner_customer_id=136,product_id=product.id,is_general=False,
            inventory_code_snapshot=product.product_code,product_name_snapshot=product.product_name,box_type_snapshot=product.box_style,
            length_mm=product.length_mm,width_mm=product.width_mm,height_mm=product.height_mm,material_code_snapshot='VSNIV',flute_type_snapshot='AB',
            physical_basis_json=None if lid==666 else product_basis(product))
        db.add(lot)
    db.commit()
    return db,actor


def execute(db,actor,**changes):
    return service.execute(db,actor=actor,preview_hash=service.preview(db,actor=actor)['preview_hash'],
        operation_key='reviewed-50-sets',evidence='用户明确从原上下盖各取50组成50套',physical_match_confirmed=True,**changes)


def test_only_child50_confirmed_assembly_and_original160_history_kept(kit):
    db,actor=kit
    before=service.preview(db,actor=actor)
    assert before['total_reference_cost']=='729.0000'
    assert db.scalar(select(func.count(InventoryLot.id)))==2
    result=execute(db,actor);assert result['quantity']==50
    assert result['reference_total_cost']=='729.0000'
    assert result['reference_unit_cost']=='14.5800'
    lower=db.get(InventoryLot,666);upper=db.get(InventoryLot,1421);child=db.get(InventoryLot,result['split_lot_id'])
    assert lower.quantity_available==upper.quantity_available==160
    assert lower.finished_detail.physical_basis_json is None
    assert child.quantity_available==0 and child.quantity_consumed==50
    assert child.finished_detail.physical_basis_json==before['expected_bases'][3496]
    assert upper.quantity_consumed==50
    edge=db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id==child.id))
    assert edge.source_lot_id==666 and edge.source_location_id==edge.target_location_id==1918
    assert edge.source_version_before==1 and edge.quantity==50
    assert {(r.lot_id,r.quantity) for r in db.scalars(select(BomAssemblyInput))}=={(child.id,50),(1421,50)}
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
    assert db.scalar(select(func.count(BomAssembly.id)))==1
    other=User(username='other-admin',password_hash='unusable',role='admin',is_active=True,real_name='其他测试管理员');db.add(other);db.flush()
    with pytest.raises(WarehouseInventoryError,match='标识'):service.execute(db,**{**kwargs,'actor':other})


@pytest.mark.parametrize('change',[{'quantity':49},{'quantity':51},{'quantity':True},{'order_item_id':10592},{'target_location_id':1}])
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
    elif change=='quantity':db.get(OrderItem,10552).quantity=51
    elif change=='basis':db.get(InventoryLot,666).finished_detail.physical_basis_json=product_basis(db.get(Product,3496))
    db.commit()
    with pytest.raises(WarehouseInventoryError):service.execute(db,actor=actor,preview_hash=p['preview_hash'],operation_key='stale',evidence='确认',physical_match_confirmed=True)
    assert db.scalar(select(func.count(InventoryLot.id)))==2


@pytest.mark.parametrize('stage',['after_assembly','audit'])
def test_unexpected_failure_rolls_back_child_basis_debits_and_output(kit,monkeypatch,stage):
    db,actor=kit
    if stage=='after_assembly':
        original=service.assemble_order_inventory
        def fail(*args,**kw):
            if kw.get('preview_only'):return original(*args,**kw)
            original(*args,**kw);raise RuntimeError('kit injected')
        monkeypatch.setattr(service,'assemble_order_inventory',fail)
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
def test_cancelled_or_finished_order_never_reopened(kit,state):
    db,actor=kit;db.get(Order,9782).status=state;db.commit()
    with pytest.raises(WarehouseInventoryError):service.preview(db,actor=actor)
    assert db.get(Order,9782).status==state
    assert db.scalar(select(func.count(InventoryLot.id)))==2


def test_corrupt_full_receipt_cannot_duplicate_assembly(kit):
    db,actor=kit;p=service.preview(db,actor=actor)
    args=dict(actor=actor,preview_hash=p['preview_hash'],operation_key='corrupt-test',evidence='确认50套',physical_match_confirmed=True)
    service.execute(db,**args)
    row=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.action==service.ACTION))
    row.response_json='{}';db.flush()
    with pytest.raises(WarehouseInventoryError,match='回执损坏'):service.execute(db,**args)
    assert db.scalar(select(func.count(BomAssembly.id)))==1


def test_wrong_source_customer_never_confirms_identity(kit):
    db,actor=kit;db.add(Customer(id=137,customer_number=137,customer_code='OTHER',name='其他客户'));db.flush()
    db.get(InventoryLot,666).finished_detail.owner_customer_id=137;db.commit()
    with pytest.raises(WarehouseInventoryError):service.preview(db,actor=actor)
    assert db.get(InventoryLot,666).finished_detail.physical_basis_json is None

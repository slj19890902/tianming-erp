"""Fictional two-customer stock: confirmation, one ledger and frozen ownership."""
import json
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedReservation
from app.services import shared_finished_stock as shared
from app.services.warehouse_inventory import (
    WarehouseInventoryError, finished_inventory_candidates_for_product, finished_inventory_candidates,
    reserve_finished_inventory, release_finished_reservation,
)
from tests.test_finished_goods_inventory_reservation import reservation_db, add_lot


def setup_pair(db, data, *, same_code=True, legacy_mold=False):
    source = data['product']
    columns = ('box_category', 'length_mm', 'width_mm', 'height_mm', 'flute_type', 'material_id',
               'report_length_mm', 'report_width_mm', 'base_report_length_mm', 'base_report_width_mm')
    target = Product(customer_id=data['other_customer'].id,
        product_code=source.product_code if same_code else 'OTHER-CODE',
        customer_material_code='OTHER-MATERIAL-CODE', product_name='虚构乙客户名称',
        sale_unit_price=Decimal('9'), **{key:getattr(source,key) for key in columns})
    db.add(target); db.flush()
    order = Order(order_number='SHARED-FICTION-B', customer_id=target.customer_id,
        order_date=date(2026,10,9),status='pending_production',payment_status='unpaid',total_amount=900)
    db.add(order); db.flush()
    item = OrderItem(order_id=order.id, product_id=target.id, quantity=100,
        unit_price=9,subtotal=900,material_status='pending',requisition_status='未报料',
        snapshot_product_code=target.product_code,snapshot_product_name=target.product_name,
        snapshot_spec='800×200×100mm',snapshot_material='A416D',snapshot_pieces_per_box=1,
        snapshot_report_length_mm=800,snapshot_report_width_mm=200)
    db.add(item)
    lot = add_lot(db,data,quantity=50,key='shared-fixture')
    if legacy_mold:
        value=json.loads(lot.finished_detail.physical_basis_json)
        value['mold_tool_id']='archived-mold'
        lot.finished_detail.physical_basis_json=shared._json(value)
    db.commit()
    return target,item,lot


def activate(db,data,target,lot):
    params=dict(product_ids=[data['product'].id,target.id],lot_ids=[lot.id])
    value=shared.preview(db,**params)
    args=dict(**params,preview_hash=value['preview_hash'],operation_key='shared-fixture-confirm',
        evidence='隔离测试：实物已经确认可给甲乙客户共用',actor=data['admin'])
    result=shared.confirm(db,**args);db.commit()
    assert shared.confirm(db,**args)==dict(group_id=result['group_id'],replayed=True)
    return result


def reserve_for(db,data,item,lot,quantity,key,version=None):
    return reserve_finished_inventory(db,order_item_id=item.id,inventory_lot_id=lot.id,
        quantity=quantity,expected_version=lot.version if version is None else version,
        operator_id=data['admin'].id,idempotency_key=key,warning_acknowledged_codes=[])


@pytest.mark.parametrize('same_code',[True,False])
@pytest.mark.parametrize('legacy_mold',[True,False])
def test_explicit_confirmation_shares_one_ledger_and_cancel_returns_to_original(reservation_db,same_code,legacy_mold):
    db,data=reservation_db
    target,item,lot=setup_pair(db,data,same_code=same_code,legacy_mold=legacy_mold)
    original=lot.finished_detail.physical_basis_json
    assert finished_inventory_candidates_for_product(db,product_id=target.id,customer_id=target.customer_id)==[]
    with pytest.raises(WarehouseInventoryError):reserve_for(db,data,item,lot,20,'denied-before-confirm')
    db.rollback()
    activate(db,data,target,lot)
    assert finished_inventory_candidates_for_product(db,product_id=target.id,customer_id=target.customer_id)==[lot]
    assert finished_inventory_candidates(db,item.id)==[lot]
    stale=lot.version
    first=reserve_for(db,data,data['item'],lot,30,'reserve-A');db.commit()
    with pytest.raises(WarehouseInventoryError):reserve_for(db,data,item,lot,30,'overbook-B',version=stale)
    db.rollback()
    second=reserve_for(db,data,item,lot,20,'reserve-B');db.commit()
    assert (lot.quantity_available,lot.quantity_reserved)==(0,50)
    assert finished_inventory_candidates(db,item.id)==[]
    assert db.get(SharedFinishedReservation,second.id).customer_id==target.customer_id
    assert lot.finished_detail.product_id==data['product'].id
    assert lot.finished_detail.owner_customer_id==data['customer'].id
    assert lot.finished_detail.physical_basis_json==original
    assert item.unit_price==9 and item.snapshot_product_name==target.product_name
    release_finished_reservation(db,reservation_id=second.id,
        operator_id=data['admin'].id,release_reason='虚构订单取消',idempotency_key='cancel-B')
    db.commit()
    assert (lot.quantity_available,lot.quantity_reserved)==(20,30)
    assert reserve_for(db,data,data['item'],lot,30,'reserve-A').id==first.id


def test_master_change_blocks_new_use_but_does_not_reinterpret_reservation(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    result=activate(db,data,target,lot)
    reservation=reserve_for(db,data,item,lot,20,'frozen-B');db.commit()
    target.print_content='仅乙客户新印刷';target.version+=1
    db.get(SharedFinishedGroup,result['group_id']).enabled=False
    db.commit()
    assert finished_inventory_candidates(db,item.id)==[]
    assert shared.reserved_match(db,reservation,lot,product_id=target.id,customer_id=target.customer_id)
    lot.finished_detail.physical_basis_json=None;db.flush()
    with pytest.raises(WarehouseInventoryError,match='冻结批次'):
        shared.reserved_match(db,reservation,lot,product_id=target.id,customer_id=target.customer_id)


def test_preview_rejects_material_or_print_difference_and_stale_and_non_admin(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    params=dict(product_ids=[data['product'].id,target.id],lot_ids=[lot.id])
    target.print_content='专用标识';db.flush()
    with pytest.raises(WarehouseInventoryError,match='印刷'):shared.preview(db,**params)
    target.print_content=None;db.flush()
    value=shared.preview(db,**params)
    args=dict(**params,preview_hash=value['preview_hash'],operation_key='stale',evidence='实物确认',actor=None)
    with pytest.raises(WarehouseInventoryError) as error:shared.confirm(db,**args)
    assert error.value.status_code==403
    lot.version+=1;db.flush()
    with pytest.raises(WarehouseInventoryError,match='已变化'):shared.confirm(db,**{**args,'actor':data['admin']})
    assert list(db.scalars(select(SharedFinishedGroup)))==[]


def test_outer_rollback_keeps_no_sharing_or_quantity_changes(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    params=dict(product_ids=[data['product'].id,target.id],lot_ids=[lot.id])
    version=lot.version;value=shared.preview(db,**params)
    shared.confirm(db,**params,preview_hash=value['preview_hash'],operation_key='rollback',evidence='虚构确认',actor=data['admin'])
    db.rollback()
    assert list(db.scalars(select(SharedFinishedGroup)))==[]
    assert lot.version==version and lot.quantity_available==50


def test_dispatch_cancel_and_cost_follow_original_lot(reservation_db):
    from app.models.delivery import Delivery, DeliveryItem
    from app.services.delivery_snapshots import build_order_delivery_snapshot
    from app.services.warehouse_inventory import consume_finished_reservation, reverse_finished_consumption
    db,data=reservation_db;target,item,lot=setup_pair(db,data,legacy_mold=True)
    activate(db,data,target,lot)
    reservation=reserve_for(db,data,item,lot,20,'ship-B');db.commit()
    delivery=Delivery(delivery_number='FICTION-SHARED-DEL',customer_id=target.customer_id,
        delivery_date=date(2026,10,9),status='dispatched',total_quantity=20,source_mode='order')
    db.add(delivery);db.flush()
    line=DeliveryItem(delivery_id=delivery.id,order_item_id=item.id,delivered_quantity=20,
        **build_order_delivery_snapshot(db,item,20))
    db.add(line);db.flush()
    mutation=consume_finished_reservation(db,reservation_id=reservation.id,stock_quantity=20,
        expected_version=lot.version,operator_id=data['admin'].id,idempotency_key='shared-consume',delivery_item_id=line.id)
    db.commit()
    assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(30,0,20)
    assert line.product_name_snapshot==target.product_name and item.unit_price==9
    reverse_finished_consumption(db,reservation_id=reservation.id,stock_quantity=20,
        expected_version=lot.version,operator_id=data['admin'].id,idempotency_key='shared-reverse',
        allocation_id=mutation.allocation.id)
    db.commit()
    assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(30,20,0)
    assert lot.finished_detail.owner_customer_id==data['customer'].id

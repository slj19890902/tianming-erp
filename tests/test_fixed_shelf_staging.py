from datetime import date, datetime
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.models.delivery import Delivery, DeliveryItem, DeliveryPickTask, DeliveryPickTaskItem
from app.models.fixed_shelf import ShelfProfile, ShelfLotState
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import (WarehouseFloor, WarehouseArea, WarehouseAreaStoragePolicy,
    WarehouseLocation, WarehouseGroundLayoutPlan, WarehouseGroundLayoutSlot, Floor3LocationLayout,
    InventoryLot, InventoryReservation, UnorderedFinishedDeliveryAllocation)
from app.services import fixed_shelf as shelf, fixed_shelf_staging as staging, location_candidates
from app.services.warehouse_inventory import WarehouseInventoryError, transfer_finished_lot_between_locations, finished_inventory_candidates_for_product
from test_fixed_shelf import setup, configure, incoming, rack_factory


def ground(db, monkeypatch):
    original = location_candidates.load_warehouse_twin_published_floor_identity
    monkeypatch.setattr(location_candidates, 'load_warehouse_twin_published_floor_identity', lambda floor:
        {'revision':'stage-map-1', 'zones_by_id':{'stage-zone':'PICK'}, 'zone_ids_by_area':{'PICK':('stage-zone',)}} if floor == 1 else original(floor))
    floor = WarehouseFloor(floor_number=1, floor_code='1F', floor_name='一楼', construction_status='enabled')
    db.add(floor); db.flush()
    area = WarehouseArea(floor_id=floor.id, area_code='PICK', area_name='固定集货区', construction_status='enabled')
    db.add(area); db.flush()
    area.storage_policy = WarehouseAreaStoragePolicy(map_feature_id='stage-zone', allowed_inventory_types_json='["finished"]',
        storage_layout='pallet_ground', status='published', published_map_revision='stage-map-1', version=1, updated_by=1)
    target = WarehouseLocation(location_code='1F-PICK-1-1', location_name='集货一号', warehouse_type='finished',
        warehouse_floor=1, area_code='PICK', storage_type='ground', placement_status='placed', source_version='CURRENT_MAP',
        address_kind='ground_slot', address_area_id=area.id, ground_row_no=1, slot_no=1)
    target.floor3_layout = Floor3LocationLayout(left_pct=10, top_pct=10, width_pct=5, height_pct=5, version=1, source_type='manual', layout_kind='physical_pallet')
    db.add(target); db.flush()
    plan = WarehouseGroundLayoutPlan(area_id=area.id, status='published', target_slot_count=1,
        numbering_origin='south', row_direction='from_aisle_inward', slot_direction='left_to_right', row_start_no=1,
        slot_start_no=1, draft_map_revision='stage-map-1', published_map_revision='stage-map-1', preview_fingerprint='a'*64,
        version=1, publish_idempotency_key='stage-ground-1', publish_request_hash='b'*64, updated_by=1, published_by=1, published_at=datetime.now())
    db.add(plan); db.flush()
    db.add(WarehouseGroundLayoutSlot(plan_id=plan.id, location_id=target.id, route_sequence=1,
        row_no=1, slot_no=1, x_mm=1000, y_mm=1000, width_mm=1200, depth_mm=1000))
    db.flush()
    assert shelf.staging_issue(db, target) is None
    return target


def case(db, pid, cid, rack_id, target, unordered=False):
    configure(db, pid, [rack_id])
    db.get(ShelfProfile, pid).staging_location_id = target.id
    lot = incoming(db, pid, cid, rack_id)
    order_item = None
    if not unordered:
        order = Order(order_number='SHELF-STAGE-ORDER', customer_id=cid, customer_po='STAGE', order_date=date.today(), delivery_date=date.today(), status='pending_delivery')
        db.add(order); db.flush()
        order_item = OrderItem(order_id=order.id, product_id=pid, quantity=85, unit_price=Decimal('1'), subtotal=85,
            requisition_status='已报料', material_status='received', delivered_quantity=0,
            snapshot_product_name='测试成品箱', snapshot_spec='800.00×200.00×100.00mm')
        db.add(order_item); db.flush()
        lot.quantity_available = 40; lot.quantity_reserved = 85
        db.add(InventoryReservation(reservation_number='STAGE-R', inventory_lot_id=lot.id, reservation_type='finished_order',
            order_id=order.id, order_item_id=order_item.id, reserved_stock_quantity=85, credited_requirement_quantity=85,
            yield_factor=1, status='active', reserved_by=1, reserved_at=datetime.now()))
    delivery = Delivery(delivery_number='SHELF-STAGE-D', customer_id=cid, delivery_date=date.today(),
        source_mode='unordered_finished' if unordered else 'order', status='pending', total_quantity=50, created_by=1)
    db.add(delivery); db.flush()
    line = DeliveryItem(delivery_id=delivery.id, order_item_id=order_item.id if order_item else None,
        product_id=pid if unordered else None, source_type='unordered_finished' if unordered else 'order', delivered_quantity=50,
        product_code_snapshot='WH-P001', product_name_snapshot='测试成品箱', specification_snapshot='800×200×100mm', unit_snapshot='只', unit_price_snapshot=1 if unordered else None, price_source='manual' if unordered else None)
    db.add(line); db.flush()
    if unordered:
        db.add(UnorderedFinishedDeliveryAllocation(delivery_item_id=line.id, inventory_lot_id=lot.id, planned_quantity=50,
            status='planned', lot_number_snapshot=lot.lot_number, warehouse_location_id_snapshot=rack_id, created_by=1))
    task = DeliveryPickTask(delivery_id=delivery.id, customer_id=cid, status='pushed', created_by=1, assigned_to=1)
    db.add(task); db.flush()
    item = DeliveryPickTaskItem(task_id=task.id, delivery_item_id=line.id, order_item_id=line.order_item_id,
        original_quantity=50, picked_quantity=0, status='pending', product_code_snapshot='WH-P001',
        product_name_snapshot='测试成品箱', specification_snapshot='800.00×200.00×100.00mm')
    db.add(item); db.commit()
    return lot, delivery, line, task, item


def test_reserved_pick_moves_only_selected_reservation_and_dispatches_staged_lot(setup, monkeypatch):
    from app.services.semi_finished_inventory import consume_delivery_item_inventory
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target)
        snapshot=shelf.staging_info(db,target)
        staging.stage_pick_item(db,item,quantity=50,target=snapshot,operator_id=1)
        db.commit()
        staged=staging.staged_lots(db,line.id)
        assert sum(x.quantity_reserved for x in staged)==50
        assert (lot.quantity_available,lot.quantity_reserved)==(40,35)
        assert all(x.warehouse_location_id==target.id for x in staged)
        assert staging.stage_pick_item(db,item,quantity=50,target=snapshot,operator_id=1)
        assert len(staging.staged_lots(db,line.id))==1
        staging.require_staged_dispatch(db,[line])
        consume_delivery_item_inventory(db,delivery_item_id=line.id,delivered_quantity_after_dispatch=50,operator_id=1,operation_key='stage-dispatch-1')
        db.commit()
        assert (lot.quantity_available,lot.quantity_reserved)==(40,35)
        assert staged[0].quantity_consumed==50
        assert staged[0].quantity_reserved==0


def test_unordered_pick_repoints_allocation_and_cannot_be_reserved_again(setup, monkeypatch):
    from app.services.unordered_finished_delivery import dispatch_unordered_finished_inventory
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target,unordered=True)
        staging.stage_pick_item(db,item,quantity=50,target=shelf.staging_info(db,target),operator_id=1)
        db.commit()
        staged=staging.staged_lots(db,line.id)[0]
        assert (lot.quantity_available,staged.quantity_available)==(75,50)
        allocation=db.scalar(select(UnorderedFinishedDeliveryAllocation).where(UnorderedFinishedDeliveryAllocation.delivery_item_id==line.id))
        assert allocation.inventory_lot_id==staged.id
        candidates=finished_inventory_candidates_for_product(db,customer_id=cid,product_id=pid)
        assert staged.id not in [x.id for x in candidates]
        staging.require_staged_dispatch(db,[line])
        dispatch_unordered_finished_inventory(db,delivery=delivery,delivery_items=[line],operator_id=1,dispatched_at=datetime.now())
        db.commit()
        assert lot.quantity_available==75 and staged.quantity_consumed==50


def test_cancel_does_not_return_goods_but_actual_return_clears_stage_marker(setup,monkeypatch):
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target,unordered=True)
        with pytest.raises(WarehouseInventoryError,match='尚未'):
            staging.require_staged_dispatch(db,[line])
        staging.stage_pick_item(db,item,quantity=50,target=shelf.staging_info(db,target),operator_id=1)
        db.commit()
        staged=staging.staged_lots(db,line.id)[0]
        with pytest.raises(shelf.ShelfError,match='不可直接'):
            staging.stage_pick_item(db,item,quantity=0,target=None,operator_id=1)
        with pytest.raises(shelf.ShelfError,match='正式移回'):
            staging.prepare_draft_replacement(db,delivery.id)
        db.delete(task); db.commit()
        assert staged.warehouse_location_id==target.id and staged.quantity_available==50
        rack=db.get(WarehouseLocation,ids[0])
        result=transfer_finished_lot_between_locations(db,lot_id=staged.id,expected_version=staged.version,quantity=50,
            location_id=rack.id,operator_id=1,idempotency_key='actual-return-1',expected_target_layout_version=rack.floor3_layout.version)
        db.commit()
        assert db.get(ShelfLotState,result.target_lot.id).staged_delivery_item_id is None
        allocation=db.scalar(select(UnorderedFinishedDeliveryAllocation).where(UnorderedFinishedDeliveryAllocation.delivery_item_id==line.id))
        assert allocation.inventory_lot_id==result.target_lot.id
        assert sum(shelf.physical_quantity(x) for x in db.scalars(select(InventoryLot)).all())==125
        staging.prepare_draft_replacement(db,delivery.id)
        assert not db.scalar(select(ShelfLotState.lot_id).where(ShelfLotState.staged_delivery_item_id==line.id))


@pytest.mark.parametrize('unordered', [False, True])
def test_mobile_partial_then_complete_and_duplicate_are_physical_once(setup,monkeypatch,unordered):
    from app.api.deliveries import (_pick_task_response, update_delivery_pick_task_item,
        complete_delivery_pick_task_as_planned, DeliveryPickItemUpdate, PickStagingBatch)
    from app.models.user import User
    from fastapi import HTTPException
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target,unordered=unordered)
        user=db.get(User,1)
        fresh=_pick_task_response(db,task)
        assert fresh['items'][0]['staging_required']
        payload=dict(pick_status='partial',picked_quantity=20,print_version='stale',staging_target=shelf.staging_info(db,target))
        with pytest.raises(HTTPException,match='') as err:
            update_delivery_pick_task_item(task.id,item.id,DeliveryPickItemUpdate(**payload),db,user)
        assert err.value.status_code==409
        assert not staging.staged_lots(db,line.id)
        payload['print_version']=fresh['print_version']
        response=update_delivery_pick_task_item(task.id,item.id,DeliveryPickItemUpdate(**payload),db,user)
        assert response['task']['items'][0]['staged_quantity']==20
        batch=PickStagingBatch(print_version=response['task']['print_version'],targets={item.id:shelf.staging_info(db,target)})
        response=complete_delivery_pick_task_as_planned(task.id,batch,db,user)
        assert response['status']=='driver_confirmed'
        assert response['items'][0]['staged_quantity']==50
        complete_delivery_pick_task_as_planned(task.id,batch,db,user)
        assert sum(shelf.physical_quantity(x) for x in staging.staged_lots(db,line.id))==50
        assert shelf.physical_quantity(lot)==75


def test_return_then_restage_uses_new_transfer_and_current_allocation(setup,monkeypatch):
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target,unordered=True)
        staging.stage_pick_item(db,item,quantity=50,target=shelf.staging_info(db,target),operator_id=1)
        db.commit()
        staged=staging.staged_lots(db,line.id)[0]
        rack=db.get(WarehouseLocation,ids[0])
        transfer_finished_lot_between_locations(db,lot_id=staged.id,expected_version=staged.version,quantity=20,
            location_id=rack.id,operator_id=1,idempotency_key='partial-return',expected_target_layout_version=rack.floor3_layout.version)
        db.commit()
        assert sum(shelf.physical_quantity(x) for x in staging.staged_lots(db,line.id))==30
        staging.stage_pick_item(db,item,quantity=50,target=shelf.staging_info(db,target),operator_id=1)
        db.commit()
        assert sum(shelf.physical_quantity(x) for x in staging.staged_lots(db,line.id))==50
        assert sum(x.planned_quantity for x in db.scalars(select(UnorderedFinishedDeliveryAllocation)).all())==50


def test_goods_already_in_staging_cell_are_split_without_moving_other_goods(setup,monkeypatch):
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target,unordered=True)
        moved=transfer_finished_lot_between_locations(db,lot_id=lot.id,expected_version=lot.version,quantity=125,
            location_id=target.id,operator_id=1,idempotency_key='whole-cell-move',expected_target_layout_version=target.floor3_layout.version)
        allocation=db.scalar(select(UnorderedFinishedDeliveryAllocation))
        allocation.inventory_lot_id=moved.target_lot.id
        db.commit()
        staging.stage_pick_item(db,item,quantity=50,target=shelf.staging_info(db,target),operator_id=1)
        db.commit()
        assert sum(shelf.physical_quantity(x) for x in staging.staged_lots(db,line.id))==50
        assert sum(shelf.physical_quantity(x) for x in db.scalars(select(InventoryLot)).all())==125


@pytest.mark.parametrize('unordered',[False,True])
def test_partial_pick_apply_and_real_dispatch_use_only_staged_goods(setup,monkeypatch,unordered):
    from app.api.deliveries import (_pick_task_response,update_delivery_pick_task_item,DeliveryPickItemUpdate,
        submit_delivery_pick_task,apply_delivery_pick_task,_dispatch_delivery)
    from app.models.user import User
    factory,pid,cid,ids=setup
    with factory() as db:
        target=ground(db,monkeypatch)
        lot,delivery,line,task,item=case(db,pid,cid,ids[0],target,unordered=unordered)
        user=db.get(User,1)
        update_delivery_pick_task_item(task.id,item.id,DeliveryPickItemUpdate(pick_status='partial',picked_quantity=20,
            print_version=_pick_task_response(db,task)['print_version'],staging_target=shelf.staging_info(db,target)),db,user)
        submit_delivery_pick_task(task.id,db,user)
        apply_delivery_pick_task(task.id,db,user)
        assert line.delivered_quantity==20
        _dispatch_delivery(delivery.id,db=db,user=user)
        assert shelf.physical_quantity(lot)==105
        assert sum(x.quantity_consumed for x in db.scalars(select(InventoryLot)).all())==20
        assert delivery.status=='dispatched'

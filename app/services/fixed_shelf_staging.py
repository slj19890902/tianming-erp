"""Physical collection before loading; dispatch remains the inventory consumer."""
from sqlalchemy import select, update, exists
from app.models.fixed_shelf import ShelfProfile, ShelfLotState
from app.models.product import Product
from app.models.order import OrderItem
from app.models.delivery import Delivery, DeliveryItem
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, WarehouseArea, UnorderedFinishedDeliveryAllocation
from app.services import fixed_shelf as shelf
from app.services.location_candidates import load_warehouse_location_projection_contexts


def held_for_staging_expression():
    return exists(select(ShelfLotState.lot_id).join(DeliveryItem, DeliveryItem.id == ShelfLotState.staged_delivery_item_id)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id).where(ShelfLotState.lot_id == InventoryLot.id, Delivery.status == 'pending'))


def staging_owner(db, lot_id):
    return db.scalar(select(ShelfLotState.staged_delivery_item_id).join(DeliveryItem, DeliveryItem.id == ShelfLotState.staged_delivery_item_id)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id).where(ShelfLotState.lot_id == lot_id, Delivery.status == 'pending'))


def prepare_draft_replacement(db, delivery_id, delivery_item_id=None):
    query = select(ShelfLotState).join(DeliveryItem,
        DeliveryItem.id == ShelfLotState.staged_delivery_item_id).where(DeliveryItem.delivery_id == delivery_id)
    if delivery_item_id is not None:
        query = query.where(DeliveryItem.id == delivery_item_id)
    states = db.scalars(query).all()
    if any(shelf.physical_quantity(db.get(InventoryLot, state.lot_id)) for state in states):
        raise shelf.ShelfError('送货单已有实际集货，请先将货物正式移回固定货架，再修改明细或删除；取消发货不会自动搬回货架')
    for state in states:
        state.staged_delivery_item_id = None
    db.flush()


def guard_unordered_pick_overlap(db, delivery_id, line_ids):
    from app.models.delivery import DeliveryPickTask
    from app.models.warehouse_inventory import FinishedGoodsInventoryDetail
    fixed = db.execute(select(UnorderedFinishedDeliveryAllocation.inventory_lot_id, ShelfProfile.product_id)
        .join(FinishedGoodsInventoryDetail, FinishedGoodsInventoryDetail.inventory_lot_id == UnorderedFinishedDeliveryAllocation.inventory_lot_id)
        .join(ShelfProfile, ShelfProfile.product_id == FinishedGoodsInventoryDetail.product_id)
        .where(UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(line_ids), UnorderedFinishedDeliveryAllocation.status == 'planned')).all()
    if not fixed:
        return
    db.execute(update(ShelfProfile).where(ShelfProfile.product_id.in_({x.product_id for x in fixed})).values(version=ShelfProfile.version))
    conflict = db.scalar(select(UnorderedFinishedDeliveryAllocation.id).join(DeliveryItem,
        DeliveryItem.id == UnorderedFinishedDeliveryAllocation.delivery_item_id).join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(DeliveryPickTask, DeliveryPickTask.delivery_id == Delivery.id).where(
        UnorderedFinishedDeliveryAllocation.inventory_lot_id.in_([x.inventory_lot_id for x in fixed]),
        UnorderedFinishedDeliveryAllocation.status == 'planned', Delivery.status == 'pending', Delivery.id != delivery_id).limit(1))
    if conflict:
        raise shelf.ShelfError('同一固定货架批次已有另一张未完成拿货任务，请先处理，避免重复找货')


def item_product(db, item):
    order_item = db.get(OrderItem, item.order_item_id) if item.order_item_id else None
    line_id = getattr(item, 'delivery_item_id', item.id)
    line = db.get(DeliveryItem, line_id) if line_id else None
    return db.get(Product, order_item.product_id if order_item else line.product_id) if order_item or line else None


def staged_lots(db, delivery_item_id):
    return list(db.scalars(select(InventoryLot).join(ShelfLotState, ShelfLotState.lot_id == InventoryLot.id)
        .where(ShelfLotState.staged_delivery_item_id == delivery_item_id, InventoryLot.status == 'active')).all())


def follow_staged_allocation_transfer(db, source_lot, target_lot, delivery_item_id):
    """Keep an unordered draft's allocation attached to goods actually moved."""
    allocation = db.scalar(select(UnorderedFinishedDeliveryAllocation).where(
        UnorderedFinishedDeliveryAllocation.delivery_item_id == delivery_item_id,
        UnorderedFinishedDeliveryAllocation.inventory_lot_id == source_lot.id,
        UnorderedFinishedDeliveryAllocation.status == 'planned'))
    if allocation is None:
        return
    take = min(allocation.planned_quantity, shelf.physical_quantity(target_lot))
    if source_lot.id != target_lot.id and take < allocation.planned_quantity:
        allocation.planned_quantity -= take
        allocation = UnorderedFinishedDeliveryAllocation(delivery_item_id=delivery_item_id,
            inventory_lot_id=target_lot.id, planned_quantity=take, status='planned', created_by=target_lot.created_by)
        db.add(allocation)
    else:
        allocation.inventory_lot_id = target_lot.id
    location = db.get(WarehouseLocation, target_lot.warehouse_location_id)
    allocation.lot_number_snapshot = target_lot.lot_number
    allocation.warehouse_location_id_snapshot = location.id
    allocation.warehouse_location_code_snapshot = location.location_code


def prioritize_staged(db, reservations, delivery_item_id):
    if delivery_item_id is None:
        return reservations
    ids = set(db.scalars(select(ShelfLotState.lot_id).where(ShelfLotState.staged_delivery_item_id == delivery_item_id)).all())
    return sorted(reservations, key=lambda row: row.inventory_lot_id not in ids) if ids else reservations


def staging_candidates(db, profile, product):
    anchor = db.get(WarehouseLocation, profile.staging_location_id) if profile.staging_location_id else None
    if anchor is None:
        return []
    rows = db.scalars(select(WarehouseLocation).where(WarehouseLocation.warehouse_floor == anchor.warehouse_floor,
        WarehouseLocation.area_code == anchor.area_code, WarehouseLocation.storage_type.in_(['ground', 'temporary_aisle']),
        WarehouseLocation.is_active.is_(True), WarehouseLocation.placement_status == 'placed').order_by(WarehouseLocation.sort_order, WarehouseLocation.id).limit(100)).all()
    contexts = load_warehouse_location_projection_contexts(db, rows)
    result = []
    for row in rows:
        info = shelf.staging_info(db, row, contexts.get(row.id, {}))
        if info['issue']:
            continue
        try:
            shelf.check_contents(db, row.id, product)
        except shelf.ShelfError:
            continue
        result.append(info)
    return result


def enrich_staging(db, item_responses, product_ids):
    profiles = {p.product_id: p for p in db.scalars(select(ShelfProfile).where(ShelfProfile.product_id.in_([p for p in product_ids.values() if p]))).all()}
    for item in item_responses:
        profile = profiles.get(product_ids.get(item['id']))
        if profile is None:
            continue
        product = db.get(Product, profile.product_id)
        lots = staged_lots(db, item['delivery_item_id'])
        item['staging_required'] = False
        anchor = db.get(WarehouseLocation, profile.staging_location_id) if profile.staging_location_id else None
        area = db.get(WarehouseArea, anchor.address_area_id) if anchor and anchor.address_area_id else None
        item['staging_area_label'] = f'{anchor.warehouse_floor}楼 · {area.area_name}' if area else None
        item['staged_quantity'] = sum(shelf.physical_quantity(lot) for lot in lots)
        item['staging_candidates'] = staging_candidates(db, profile, product)
        item['staging_default_location_id'] = profile.staging_location_id


def stage_pick_item(db, item, *, quantity, target, operator_id):
    from app.api.deliveries import _pick_item_response
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations
    product = item_product(db, item)
    profile = db.get(ShelfProfile, product.id) if product else None
    if profile is None:
        return False
    current = sum(shelf.physical_quantity(lot) for lot in staged_lots(db, item.delivery_item_id))
    if quantity < current:
        raise shelf.ShelfError('已有货物移入集货位，请先按实际退回数量做正式移库，不可直接改成少拿或无货')
    remaining = quantity - current
    if remaining == 0:
        return True
    if not target:
        raise shelf.ShelfError('请先配置固定集货区，并选择货物实际放置的集货位')
    anchor = db.get(WarehouseLocation, profile.staging_location_id) if profile.staging_location_id else None
    location = db.get(WarehouseLocation, target.get('location_id'))
    if not anchor or not location or (location.warehouse_floor, location.area_code) != (anchor.warehouse_floor, anchor.area_code):
        raise shelf.ShelfError('请选择本料号配置的固定集货区内的实际货位')
    issue = shelf.staging_issue(db, location)
    if issue:
        raise shelf.ShelfError(issue)
    if location.address_version != target.get('address_version'):
        raise shelf.ShelfError('集货位地址已变化，请刷新后重新核对')
    shelf.check_contents(db, location.id, product)
    source_lines = _pick_item_response(db, item)['location_lines']
    for line in source_lines:
        if remaining <= 0:
            break
        if line.get('source_type') != 'finished_inventory' or not line.get('lot_id'):
            continue
        source = db.get(InventoryLot, line['lot_id'], populate_existing=True)
        state = db.get(ShelfLotState, source.id)
        if state and state.staged_delivery_item_id == item.delivery_item_id:
            continue
        if state and state.staged_delivery_item_id:
            old_line = db.get(DeliveryItem, state.staged_delivery_item_id)
            old_delivery = db.get(Delivery, old_line.delivery_id) if old_line else None
            if old_delivery and old_delivery.status == 'pending':
                raise shelf.ShelfError('该批次已为另一张送货单集货，不能重复拿取')
        if source.finished_detail.product_id != product.id:
            raise shelf.ShelfError('组合子件来源不能按主品直接集货，请先核对独立成品来源')
        take = min(remaining, int(line.get('pick_quantity') or 0))
        if not take:
            continue
        reservation_id = line.get('reservation_id')
        allocation = None
        if not reservation_id:
            allocation = db.scalar(select(UnorderedFinishedDeliveryAllocation).where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id == item.delivery_item_id,
                UnorderedFinishedDeliveryAllocation.inventory_lot_id == source.id,
                UnorderedFinishedDeliveryAllocation.status == 'planned'))
            if allocation is None:
                raise shelf.ShelfError('缺少可靠的成品拿货分配，不能直接确认集货')
        result = transfer_finished_lot_between_locations(db, lot_id=source.id, expected_version=source.version,
            quantity=take, location_id=location.id, operator_id=operator_id,
            idempotency_key=f'shelf-stage-{item.id}-{current}-{source.id}-{source.version}-{reservation_id or 0}',
            expected_source_location_id=source.warehouse_location_id,
            expected_target_address_version=target['address_version'], expected_target_layout_version=target.get('layout_version'),
            reserved_plan={int(reservation_id): take} if reservation_id else {})
        staged = result.target_lot
        state = db.get(ShelfLotState, staged.id)
        if state is None:
            state = ShelfLotState(lot_id=staged.id, units_per_bundle=None)
            db.add(state)
        state.staged_delivery_item_id = item.delivery_item_id
        state.target_location_id = None
        if allocation:
            if allocation.planned_quantity == take:
                allocation.inventory_lot_id = staged.id
                moved = allocation
            else:
                allocation.planned_quantity -= take
                moved = UnorderedFinishedDeliveryAllocation(delivery_item_id=item.delivery_item_id,
                    inventory_lot_id=staged.id, planned_quantity=take, status='planned', created_by=operator_id)
                db.add(moved)
            moved.lot_number_snapshot = staged.lot_number
            moved.warehouse_location_id_snapshot = location.id
            moved.warehouse_location_code_snapshot = location.location_code
        db.flush()
        remaining -= take
        current += take
    if remaining:
        raise shelf.ShelfError('可定位成品来源不足，不能将未集货的数量标为拿齐')
    return True


def require_staged_dispatch(db, lines):
    from app.services.warehouse_inventory import WarehouseInventoryError
    for line in lines:
        product = item_product(db, line)
        profile = db.get(ShelfProfile, product.id) if product else None
        if profile is None:
            continue
        actual_staged = staged_lots(db, line.id)
        if not actual_staged:
            from app.models.delivery import DeliveryPickTaskItem
            picked = db.scalars(select(DeliveryPickTaskItem).where(
                DeliveryPickTaskItem.delivery_item_id == line.id)).all()
            if not picked or not any(item.status in {'picked', 'partial'} and int(item.picked_quantity or 0) >= int(line.delivered_quantity) for item in picked):
                raise WarehouseInventoryError('固定货架拿货尚未完成，请先核对拿货数量', 409)
            continue
        anchor = db.get(WarehouseLocation, profile.staging_location_id) if profile.staging_location_id else None
        valid = 0
        for lot in actual_staged:
            location = db.get(WarehouseLocation, lot.warehouse_location_id)
            if anchor and location and (location.warehouse_floor, location.area_code) == (anchor.warehouse_floor, anchor.area_code) and not shelf.staging_issue(db, location):
                valid += int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
        if valid < line.delivered_quantity:
            raise WarehouseInventoryError('固定货架成品尚未按本次送货数量确认集货，请先完成拿货并核对实际集货位', 409)

"""Physical sheet moves: keep ledger, applicability, costs and reservations together."""
from sqlalchemy import select
from app.core.time_contract import utc_now_naive
from app.models.warehouse_inventory import (
    InventoryLot, InventoryLotTransfer, InventoryReservation,
    SemiFinishedInventoryDetail, SemiFinishedLotAllowedProduct,
)
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.services import warehouse_inventory as inv


def _copy_columns(row, excluded):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns
            if column.key not in excluded}


def transfer_sheet_lot_between_locations(db, **args):
    key = args['idempotency_key'].strip()
    quantity = args['quantity']
    if not key or len(key) > 120 or quantity <= 0:
        raise inv.WarehouseInventoryError('移动数量或请求标识无效', 422)
    hash_args = {name: args.get(name) for name in (
        'lot_id', 'expected_version', 'quantity', 'location_id',
        'expected_source_location_id', 'expected_source_address_version',
        'expected_source_layout_version', 'expected_source_map_revision',
        'expected_target_layout_version', 'expected_target_address_version',
        'expected_target_map_revision', 'ground_secondary_location_id', 'ground_capacity_quantity')}
    digest = inv._lot_location_transfer_hash(**hash_args)

    def replay():
        old = db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.idempotency_key == key))
        if old is not None:
            return inv._replayed_finished_location_transfer(db, repeated=old,
                lot_id=args['lot_id'], location_id=args['location_id'], quantity=quantity,
                expected_version=args['expected_version'], request_hash=digest,
                compatible_legacy_hashes=(), expected_source_location_id=args.get('expected_source_location_id'))

    repeated = replay()
    if repeated:
        return repeated
    lot = db.get(InventoryLot, args['lot_id'])
    if lot is None:
        raise inv.WarehouseInventoryError('片料批次不存在', 404)
    source_id = args.get('expected_source_location_id') or lot.warehouse_location_id
    inv._claim_inventory_transfer_locations(db, source_location_id=source_id,
        target_location_id=args['location_id'],
        expected_source_layout_version=args.get('expected_source_layout_version'),
        expected_target_layout_version=args.get('expected_target_layout_version'))
    repeated = replay()
    if repeated:
        return repeated
    db.refresh(lot)
    if (lot.inventory_type != 'semi_finished' or lot.semi_finished_detail is None
            or lot.status != 'active' or lot.quantity_damaged or lot.pallet_item is not None):
        raise inv.WarehouseInventoryError('片料状态或栈板关系异常，请先核对', 409)
    if lot.version != args['expected_version'] or lot.warehouse_location_id != source_id:
        raise inv.WarehouseInventoryError('库存版本或位置已变化，请刷新重试', 409)
    if source_id == args['location_id']:
        raise inv.WarehouseInventoryError('目标位置不能与来源位置相同', 409)
    from app.services.warehouse_stocktake_batch import (
        assert_location_add_compatible, _location_live_lots, WarehouseStocktakeBatchError)
    source = inv._location(db, source_id, 'semi_finished', capacity_source_location_id=source_id)
    occupied = bool(_location_live_lots(db, args['location_id']))
    target = inv._location(db, args['location_id'], 'semi_finished',
        capacity_source_location_id=args['location_id'] if occupied else source_id)
    for location, role, prefix in ((source, '来源', 'source'), (target, '目标', 'target')):
        inv._validate_transfer_location_snapshot(db, location=location, role=role,
            expected_address_version=args.get(f'expected_{prefix}_address_version'),
            expected_layout_version=args.get(f'expected_{prefix}_layout_version'),
            expected_map_revision=args.get(f'expected_{prefix}_map_revision'))
    try:
        assert_location_add_compatible(db, target.id)
    except WarehouseStocktakeBatchError as error:
        raise inv.WarehouseInventoryError(str(error), error.status_code) from error
    from app.models.user import User
    operator = db.get(User, args['operator_id'])
    if operator is None or operator.role != 'admin':
        source_facts = _copy_columns(lot.semi_finished_detail, {'inventory_lot_id'})
        for existing in _location_live_lots(db, target.id):
            if (existing.inventory_type != 'semi_finished' or existing.semi_finished_detail is None
                    or _copy_columns(existing.semi_finished_detail, {'inventory_lot_id'}) != source_facts):
                raise inv.WarehouseInventoryError('不同货物混放请由管理员操作', 403)
    before = inv._balances(lot)
    total = before['available'] + before['reserved']
    if quantity > total:
        raise inv.WarehouseInventoryError(f'当前仅有 {total} 张可搬运', 409)
    available_take = min(quantity, before['available'])
    reserved_take = quantity - available_take
    now = utc_now_naive()
    if quantity == total:
        lot.warehouse_location_id = target.id
        target_lot = lot
    else:
        target_lot = InventoryLot(**_copy_columns(lot, {
            'id', 'lot_number', 'warehouse_location_id', 'quantity_available', 'quantity_reserved',
            'quantity_consumed', 'quantity_damaged', 'quantity_scrapped', 'version',
            'created_at', 'updated_at', 'created_by', 'last_movement_at', 'source_type', 'remarks'}),
            lot_number=inv._number('SI'), warehouse_location_id=target.id,
            quantity_available=available_take, quantity_reserved=reserved_take,
            source_type='transfer', created_by=args['operator_id'], last_movement_at=now,
            remarks=f'由批次 {lot.lot_number} 移位拆分')
        target_lot.semi_finished_detail = SemiFinishedInventoryDetail(
            **_copy_columns(lot.semi_finished_detail, {'inventory_lot_id'}))
        db.add(target_lot)
        db.flush()
        profile = db.get(WarehouseGoodsProfile, lot.id)
        if profile:
            db.add(WarehouseGoodsProfile(lot_id=target_lot.id, data_json=profile.data_json))
        for binding in lot.allowed_products:
            db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=target_lot.id,
                **_copy_columns(binding, {'id', 'inventory_lot_id'})))
        remainder = reserved_take
        reservations = db.scalars(select(InventoryReservation).where(
            InventoryReservation.inventory_lot_id == lot.id,
            InventoryReservation.status.in_(('active', 'partial'))).order_by(InventoryReservation.id)).all()
        for reservation in reservations:
            outstanding = reservation.reserved_stock_quantity - reservation.consumed_stock_quantity - reservation.released_stock_quantity
            take = min(remainder, outstanding)
            if take <= 0:
                continue
            outstanding_pieces = reservation.credited_requirement_quantity - reservation.consumed_requirement_quantity - reservation.released_requirement_quantity
            # Leave any rounded final-sheet excess with the retained reservation.
            credit = min(outstanding_pieces, take * reservation.yield_factor)
            db.add(InventoryReservation(**_copy_columns(reservation, {
                'id', 'reservation_number', 'inventory_lot_id', 'reserved_stock_quantity',
                'credited_requirement_quantity', 'consumed_stock_quantity', 'consumed_requirement_quantity',
                'released_stock_quantity', 'released_requirement_quantity', 'released_by', 'released_at',
                'release_reason', 'status', 'idempotency_key', 'reservation_group_key',
                'reservation_group_requested_quantity'}),
                reservation_number=inv._number('RS'), inventory_lot_id=target_lot.id,
                reserved_stock_quantity=take, credited_requirement_quantity=credit,
                status='active', idempotency_key=inv._transfer_key('sheet-move', key, reservation.id),
                reservation_group_key=inv._transfer_key('sheet-move', key, reservation.id),
                reservation_group_requested_quantity=take))
            reservation.released_stock_quantity += take
            reservation.released_requirement_quantity += credit
            reservation.released_by = args['operator_id']
            reservation.released_at = now
            reservation.release_reason = '片料位置移动拆分'
            reservation.status = inv._finished_reservation_status(reservation)
            remainder -= take
        if remainder:
            raise inv.WarehouseInventoryError('片料预占明细与余额不一致', 409)
        lot.quantity_available -= available_take
        lot.quantity_reserved -= reserved_take
    lot.version += 1
    lot.last_movement_at = now
    transfer = InventoryLotTransfer(source_lot_id=lot.id, target_lot_id=target_lot.id,
        source_location_id=source_id, target_location_id=target.id, quantity=quantity,
        available_quantity=available_take, reserved_quantity=reserved_take,
        source_version_before=args['expected_version'], source_version_after=lot.version,
        idempotency_key=key, request_hash=digest, transferred_by=args['operator_id'], transferred_at=now)
    db.add(transfer)
    inv._movement(db, lot=lot, movement_type='location_transfer', quantity=quantity,
        before=before, operator_id=args['operator_id'], reason='片料移位',
        idempotency_key=inv._transfer_key('sheet-move', key, 'source'))
    if target_lot is not lot:
        inv._movement(db, lot=target_lot, movement_type='location_transfer', quantity=quantity,
            before={name: 0 for name in before}, operator_id=args['operator_id'], reason='片料移位',
            idempotency_key=inv._transfer_key('sheet-move', key, 'target'))
    db.flush()
    return inv.FinishedLotLocationTransferResult(transfer, lot, target_lot, False)

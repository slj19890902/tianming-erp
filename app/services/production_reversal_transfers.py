"""Reverse an intact completion's proven transfer family at its current places."""
from sqlalchemy import select, update, func
from app.models.warehouse_inventory import (InventoryLot, InventoryLotTransfer, InventoryMovement,
    InventoryReservation, InventoryPallet, InventoryPalletItem)


def transfer_family(db, roots):
    ids = set(roots) - {None}
    transfers, frontier = {}, ids.copy()
    while frontier:
        rows = list(db.scalars(select(InventoryLotTransfer).where(InventoryLotTransfer.source_lot_id.in_(frontier))))
        next_ids = {r.target_lot_id for r in rows} - ids
        transfers.update({r.id: r for r in rows})
        ids.update(next_ids)
        frontier = next_ids
    return ids, sorted(transfers.values(), key=lambda r: r.id)


def reverse_transferred_completion(db, *, completion, operator_id, reason, source_ref_type='production_completion'):
    from app.services.production_workflow import ProductionWorkflowError, _stable_key
    from app.services.warehouse_inventory import _balances, _movement, _transfer_key, release_finished_reservation
    from app.services.bom_subkit_inventory import _only_reversed_graph_consumptions
    from app.core.time_contract import utc_now_naive
    ids, transfers = transfer_family(db, [completion.inventory_lot_id])
    if not transfers:
        return None
    def fail(message):
        raise ProductionWorkflowError(message, 409)
    lots = list(db.scalars(select(InventoryLot).where(InventoryLot.id.in_(ids)).order_by(InventoryLot.id)))
    root = db.get(InventoryLot, completion.inventory_lot_id)
    if root is None or len(lots) != len(ids):
        fail('移库来源批次不完整，不能撤销生产')
    quantity = int(completion.stock_quantity or completion.quantity or 0)
    if sum(r.quantity_available + r.quantity_reserved for r in lots) != quantity:
        fail('本次完工在各货位的合计库存已变化，请先撤销后续使用')
    for lot in lots:
        if root.finished_detail is None or lot.finished_detail is None or any(
            getattr(lot.finished_detail, field) != getattr(root.finished_detail, field)
            for field in ('product_id', 'owner_customer_id', 'is_general', 'physical_basis_json')):
            fail('移库批次产品身份不一致，不能撤销')
        if (lot.inventory_type != 'finished' or lot.source_ref_type != source_ref_type
            or lot.source_ref_id != completion.id or lot.unit != root.unit
            or lot.status not in ('active', 'closed')
            or lot.quantity_consumed or lot.quantity_damaged or lot.quantity_scrapped
            or lot.estimated_unit_cost_snapshot != root.estimated_unit_cost_snapshot
            or lot.cost_snapshot_detail_json != root.cost_snapshot_detail_json):
            fail(f'批次 {lot.lot_number} 来源、成本或库存状态已变化，不能撤销')
    movements = list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id.in_(ids)).order_by(InventoryMovement.id)))
    keyed = {r.idempotency_key: r for r in movements}
    proven = set()
    for transfer in transfers:
        roles = [('source', transfer.source_lot_id)]
        if transfer.source_lot_id != transfer.target_lot_id:
            roles.append(('target', transfer.target_lot_id))
        for role, lot_id in roles:
            movement = keyed.get(_transfer_key('location-transfer', transfer.idempotency_key, role))
            if (movement is None or movement.inventory_lot_id != lot_id
                or movement.movement_type != 'location_transfer' or movement.quantity != transfer.quantity
                or movement.unit != root.unit):
                fail('移库记录缺少匹配的库存流水，不能按数量猜测撤销')
            sign = 0 if transfer.source_lot_id == transfer.target_lot_id else (-1 if role == 'source' else 1)
            if (movement.after_available - movement.before_available != sign * transfer.available_quantity
                or movement.after_reserved - movement.before_reserved != sign * transfer.reserved_quantity
                or any(getattr(movement, 'before_'+field) != getattr(movement, 'after_'+field)
                    for field in ('consumed', 'damaged', 'scrapped'))):
                fail('移库正反数量不一致，不能撤销生产')
            proven.add(movement.id)
    # Verify the full ledger chain, not just equal final balances.
    for lot in lots:
        ledger = [m for m in movements if m.inventory_lot_id == lot.id]
        balances = dict(available=0, reserved=0, consumed=0, damaged=0, scrapped=0)
        if not ledger or (lot.id == root.id and ledger[0].movement_type != 'manual_in') or (lot.id != root.id and ledger[0].id not in proven):
            fail('移库批次缺少原始入库依据')
        for movement in ledger:
            if any(getattr(movement, 'before_'+field) != value for field, value in balances.items()):
                fail('移库批次流水数量不连续，不能撤销')
            balances = {field: getattr(movement, 'after_'+field) for field in balances}
        if any(getattr(lot, 'quantity_'+field) != value for field, value in balances.items()):
            fail('移库批次数量与流水不一致，不能撤销')
        if not _only_reversed_graph_consumptions(db, lot, allow_initial_reserve=True,
            proven_transfer_movement_ids=proven):
            fail(f'批次 {lot.lot_number} 仍有未撤销或无法核对的后续流水')
    reservations = list(db.scalars(select(InventoryReservation).where(InventoryReservation.inventory_lot_id.in_(ids))))
    for lot in lots:
        active = [r for r in reservations if r.inventory_lot_id == lot.id and
            r.reserved_stock_quantity - r.consumed_stock_quantity - r.released_stock_quantity > 0]
        if (sum(r.reserved_stock_quantity-r.consumed_stock_quantity-r.released_stock_quantity for r in active) != lot.quantity_reserved
            or any(r.order_item_id != completion.order_item_id or r.reservation_type != 'finished_order'
                or r.consumed_stock_quantity for r in active)):
            fail(f'批次 {lot.lot_number} 已绑定其他订单或预占数量不一致')
    for row in reservations:
        if row.reserved_stock_quantity > row.consumed_stock_quantity + row.released_stock_quantity:
            release_finished_reservation(db, reservation_id=row.id, operator_id=operator_id,
                release_reason=reason, idempotency_key=_stable_key('completion-family-release', source_ref_type, completion.id, row.id),
                allow_downstream=True, allow_production_reversal=True)
    close_movements, pallet_ids = [], set()
    for lot in lots:
        db.refresh(lot)
        if lot.pallet_item:
            pallet_ids.add(lot.pallet_item.pallet_id)
        quantity = int(lot.quantity_available)
        if not quantity:
            continue
        before, now = _balances(lot), utc_now_naive()
        claimed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id,
            InventoryLot.version == lot.version, InventoryLot.quantity_available == quantity,
            InventoryLot.quantity_reserved == 0).values(quantity_available=0, status='closed',
                version=InventoryLot.version+1, last_movement_at=now))
        if claimed.rowcount != 1:
            fail('移库后的库存已变化，请重新预览撤销')
        db.refresh(lot)
        close_movements.append(_movement(db, lot=lot, movement_type='adjust', quantity=quantity,
            before=before, operator_id=operator_id, reason='撤销完工移库批次：'+reason,
            idempotency_key=_stable_key('completion-family-close', source_ref_type, completion.id, lot.id),
            related_order_item_id=completion.order_item_id))
    from app.services.floor3_locations import clear_pallet
    from app.services.warehouse_ground_slots import release_ground_occupancy_for_pallet
    for pid in pallet_ids:
        pallet = db.get(InventoryPallet, pid)
        balance = db.scalar(select(func.coalesce(func.sum(InventoryLot.quantity_available + InventoryLot.quantity_reserved +
            InventoryLot.quantity_damaged), 0)).join(InventoryPalletItem,
                InventoryPalletItem.inventory_lot_id == InventoryLot.id).where(InventoryPalletItem.pallet_id == pid))
        if pallet and pallet.is_current and balance == 0:
            clear_pallet(db, pallet_id=pid, expected_version=pallet.version, remarks=reason, operator_id=operator_id)
            release_ground_occupancy_for_pallet(db, pallet_id=pid, operator_id=operator_id)
    db.flush()
    return close_movements[0]

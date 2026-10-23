"""Reduce unused order sheet reservations, keeping the original ledger intact."""
import json

from sqlalchemy import select

from app.models.audit import OperationLog
from app.models.order import OrderItem
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement, WarehouseLocation
from app.services.warehouse_inventory import WarehouseInventoryError


def describe(db, state, user):
    from app.api import warehouse as api
    from app.services.production_workflow import has_production_completion_facts
    snapshot, item, requirement, preview = state
    query = select(InventoryReservation).join(OrderItemSemiRequirement,
        OrderItemSemiRequirement.id == InventoryReservation.semi_requirement_id).where(
        InventoryReservation.order_item_id == item.id,
        InventoryReservation.reservation_type == 'semi_order',
        OrderItemSemiRequirement.component_type == requirement.component_type,
        InventoryReservation.sales_order_item_bom_component_id == snapshot.id if snapshot else
        InventoryReservation.sales_order_item_bom_component_id.is_(None))
    completed = has_production_completion_facts(db, [item.id])
    rows = []
    revision = []
    for reservation in db.scalars(query.order_by(InventoryReservation.id)):
        remaining = (reservation.reserved_stock_quantity - reservation.consumed_stock_quantity
                     - reservation.released_stock_quantity)
        if remaining <= 0:
            continue
        api._require_reservation_customer_access(db, reservation.id, user)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError('片料预占来源缺失，请核对原记录', 409)
        detail = lot.semi_finished_detail
        location = db.get(WarehouseLocation, lot.warehouse_location_id)
        reason = ('已有生产完工事实，请从原完工记录核对' if completed else
                  '本批已有消耗，请从原生产记录核对' if reservation.consumed_stock_quantity else '')
        rows.append(dict(reservation_id=reservation.id, lot_id=lot.id, version=lot.version,
            remaining_stock_quantity=remaining, keep_stock_quantity=remaining,
            can_adjust=not reason, blocked_reason=reason,
            board_length_mm=detail.board_length_mm if detail else None,
            board_width_mm=detail.board_width_mm if detail else None,
            material_code=detail.material_code_snapshot if detail else '',
            location_name=(location.location_name or location.location_code) if location else '位置待核对'))
        revision.append([reservation.id, lot.id, lot.version, reservation.reserved_stock_quantity,
            reservation.consumed_stock_quantity, reservation.released_stock_quantity,
            reservation.credited_requirement_quantity, reservation.consumed_requirement_quantity,
            reservation.released_requirement_quantity, reservation.status, reservation.cut_plan_json])
    procurement = api._manual_sheet_procurement_state(db, item, requirement.component_type, snapshot=snapshot)
    return {**preview, 'existing_reservations': rows,
        'adjustment_revision': api._manual_sheet_digest([preview['expected_order_item_revision'],
            snapshot.id if snapshot else None, requirement.component_type,
            requirement.required_piece_quantity, preview['remaining_required_piece_qty'],
            preview['semi_finished_reserved_piece_qty'], procurement['revision'], completed, revision])}


def adjust(db, target_id, payload, user, *, is_bom):
    from app.api import warehouse as api
    from app.services.audit_log import append_audit_event
    from app.services.production_workflow import lock_order_rows_for_production_transition, ProductionWorkflowError
    from app.services.semi_finished_inventory import release_semi_finished_reservation
    snapshot = db.get(SalesOrderItemBomComponent, target_id) if is_bom else None
    if is_bom and snapshot is None:
        raise WarehouseInventoryError('组件快照不存在', 404)
    item_id = snapshot.sales_order_item_id if snapshot else target_id
    api._require_order_item_customer_access(db, item_id, user)
    for row in payload.items:
        api._require_reservation_customer_access(db, row.reservation_id, user)
    action = 'bom_manual_sheet_adjust' if is_bom else 'order_manual_sheet_adjust'
    operation_id = api._manual_sheet_digest(payload.idempotency_key)
    digest = api._manual_sheet_digest(dict(target_id=target_id, is_bom=is_bom, **payload.model_dump()))
    item = db.get(OrderItem, item_id)
    if item is None:
        raise WarehouseInventoryError('订单明细不存在', 404)
    try:
        lock_order_rows_for_production_transition(db, [item.order_id])
    except ProductionWorkflowError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error
    previous = db.scalar(select(OperationLog).where(OperationLog.action_code == action,
        OperationLog.request_id == operation_id))
    if previous:
        saved = json.loads(previous.details)
        if previous.user_id != user.id or saved['request_digest'] != digest:
            raise WarehouseInventoryError('同一调整凭证不能更改批次或保留张数', 409)
        return {**saved['response'], 'replayed': True}
    builder = api._bom_manual_sheet_state if is_bom else api._order_manual_sheet_state
    state = builder(db, target_id, payload.component_type, user, 'auto')
    preview = describe(db, state, user)
    if preview['adjustment_revision'] != payload.expected_adjustment_revision:
        raise WarehouseInventoryError('库存、用料或采购状态已变化，请刷新后重新调整', 409)
    current = {row['reservation_id']: row for row in preview['existing_reservations']}
    if len({row.reservation_id for row in payload.items}) != len(payload.items):
        raise WarehouseInventoryError('同一片料安排不能重复调整', 409)
    changes = []
    for selected in payload.items:
        row = current.get(selected.reservation_id)
        if row is None:
            raise WarehouseInventoryError('片料安排不属于本次订单或组件', 409)
        if not row['can_adjust']:
            raise WarehouseInventoryError(row['blocked_reason'], 409)
        if selected.keep_stock_quantity > row['remaining_stock_quantity']:
            raise WarehouseInventoryError('增加采用量请重新匹配可用片料；本操作只减少未消耗安排', 409)
        quantity = row['remaining_stock_quantity'] - selected.keep_stock_quantity
        if quantity:
            changes.append((selected, row, quantity))
    if not changes:
        raise WarehouseInventoryError('本次没有修改采用张数', 409)
    versions = {row['lot_id']: row['version'] for _, row, _ in changes}
    for selected, row, quantity in changes:
        # Two reservations can share one physical lot. Validate the whole initial
        # snapshot first, then use the version advanced by this transaction only.
        release_semi_finished_reservation(db, reservation_id=selected.reservation_id,
            expected_version=versions[row['lot_id']], operator_id=user.id, stock_quantity=quantity,
            release_reason='调整未加工片料采用量，差额重新报料',
            idempotency_key='sheet-adjust-' + api._manual_sheet_digest([operation_id, selected.reservation_id])[:60])
        versions[row['lot_id']] += 1
    fresh = builder(db, target_id, payload.component_type, user, 'auto')[3]
    response = dict(released_stock_quantity=sum(quantity for _, _, quantity in changes),
        remaining_required_piece_qty=fresh['remaining_required_piece_qty'],
        semi_finished_reserved_piece_qty=fresh['semi_finished_reserved_piece_qty'],
        committed_piece_qty=fresh['committed_piece_qty'], order_item_id=item_id,
        changes=[dict(reservation_id=selected.reservation_id, before=row['remaining_stock_quantity'],
                      after=selected.keep_stock_quantity) for selected, row, _ in changes],
        message=f"片料安排已调整，剩余 {fresh['remaining_required_piece_qty']} 片需补充用料或报料")
    append_audit_event(db, event_category='business', result='success', source='web',
        module_code='warehouse', action_code=action, resource='semi_finished_inventory', actor=user,
        entity_type='bom_component' if is_bom else 'order_item', entity_id=target_id,
        customer_id=state[2].customer_id, request_id=operation_id,
        description='调整尚未消耗的库存片料采用量', details={'request_digest': digest, 'response': response})
    db.flush()
    return response

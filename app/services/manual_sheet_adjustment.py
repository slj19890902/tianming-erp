"""Adjust unused physical sheet input while preserving order coverage and ledgers."""
import json

from sqlalchemy import select, update

from app.models.audit import OperationLog
from app.models.order import OrderItem
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement, WarehouseLocation
from app.services.warehouse_inventory import WarehouseInventoryError


def _growth_candidate(db, reservation, candidates):
    candidate = candidates.get(reservation.inventory_lot_id)
    plan = json.loads(reservation.cut_plan_json or '{}')
    frozen = {key: value for key, value in plan.items()
        if key not in {'review_contract', 'full_sheet_output_v1', 'actual_sheet_input_v1'}}
    if 'review_contract' in plan:
        from app.services.semi_finished_inventory import _cut_review_contract
        requirement = db.get(OrderItemSemiRequirement, reservation.semi_requirement_id)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if plan['review_contract'] != _cut_review_contract(db, lot, requirement, reservation.warning_acknowledged_by):
            return None
    return candidate if (candidate and candidate['selectable']
        and candidate.get('extra_stock_allowed') and plan.get('full_sheet_output_v1') is True
        and reservation.requirement_quantity_denominator == 1
        and candidate.get('cut_plan') == frozen) else None


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
    adjustment_preview = preview
    if preview['match_mode'] != 'auto':
        builder = api._bom_manual_sheet_state if snapshot else api._order_manual_sheet_state
        adjustment_preview = builder(db, snapshot.id if snapshot else item.id,
            requirement.component_type, user, 'auto')[3]
    candidates = {row['lot_id']: row for row in adjustment_preview['items']}
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
        candidate = _growth_candidate(db, reservation, candidates) if not reason else None
        rows.append(dict(reservation_id=reservation.id, lot_id=lot.id, version=lot.version,
            remaining_stock_quantity=remaining, keep_stock_quantity=remaining,
            can_adjust=not reason, blocked_reason=reason,
            can_increase=bool(candidate), available_stock_quantity=int(lot.quantity_available),
            max_stock_quantity=remaining + (int(lot.quantity_available) if candidate else 0),
            stock_yield_per_sheet=int(reservation.yield_factor or 1),
            credited_requirement_quantity=max(int(reservation.credited_requirement_quantity or 0)
                - int(reservation.released_requirement_quantity or 0), 0),
            warning_codes=candidate['warning_codes'] if candidate else [],
            warning_messages=candidate['warning_messages'] if candidate else [],
            increase_blocked_reason='' if candidate else '增加用量需有当前有效的整张加工依据，请在下方重新选择可用片料',
            board_length_mm=detail.board_length_mm if detail else None,
            board_width_mm=detail.board_width_mm if detail else None,
            material_code=detail.material_code_snapshot if detail else '',
            location_name=(location.location_name or location.location_code) if location else '位置待核对'))
        revision.append([reservation.id, lot.id, lot.version, reservation.reserved_stock_quantity,
            reservation.consumed_stock_quantity, reservation.released_stock_quantity,
            reservation.credited_requirement_quantity, reservation.consumed_requirement_quantity,
            reservation.released_requirement_quantity, reservation.status, reservation.cut_plan_json,
            candidate['review_hash'] if candidate else None])
    procurement = api._manual_sheet_procurement_state(db, item, requirement.component_type, snapshot=snapshot)
    return {**preview, 'existing_reservations': rows,
        'adjustment_revision': api._manual_sheet_digest([preview['expected_order_item_revision'],
            snapshot.id if snapshot else None, requirement.component_type,
            requirement.required_piece_quantity, preview['remaining_required_piece_qty'],
            preview['semi_finished_reserved_piece_qty'], procurement['revision'], completed, revision])}


def _increase(db, reservation, quantity, credit, version, user, key):
    from app.services.warehouse_inventory import _balances, _movement, utc_now
    from app.services.semi_finished_inventory import _reservation_status
    lot = db.get(InventoryLot, reservation.inventory_lot_id, populate_existing=True)
    if lot.version != version:
        raise WarehouseInventoryError('库存版本已变化，请刷新后重新调整', 409)
    before = _balances(lot)
    now = utc_now()
    changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id,
        InventoryLot.version == version, InventoryLot.status == 'active',
        InventoryLot.inventory_type == 'semi_finished', InventoryLot.quantity_available >= quantity).values(
        quantity_available=InventoryLot.quantity_available - quantity,
        quantity_reserved=InventoryLot.quantity_reserved + quantity,
        version=InventoryLot.version + 1, last_movement_at=now))
    if changed.rowcount != 1:
        raise WarehouseInventoryError('追加张数超过可用库存或库存状态已变化', 409)
    reservation.reserved_stock_quantity += quantity
    reservation.credited_requirement_quantity += credit
    plan = json.loads(reservation.cut_plan_json)
    plan['actual_sheet_input_v1'] = True
    reservation.cut_plan_json = json.dumps(plan, ensure_ascii=False, sort_keys=True)
    reservation.status = _reservation_status(reservation)
    db.flush()
    db.expire(lot)
    _movement(db, lot=db.get(InventoryLot, lot.id), movement_type='reserve', quantity=quantity,
        before=before, operator_id=user.id, reason='追加本次整张片料加工安排，订单覆盖封顶',
        idempotency_key=key, reservation_id=reservation.id,
        related_order_id=reservation.order_id, related_order_item_id=reservation.order_item_id)


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
    request = payload.model_dump()
    # Preserve replay of v718 adjustment requests before warning fields existed.
    for row in request['items']:
        if not row['warning_acknowledged_codes']:
            row.pop('warning_acknowledged_codes')
    digest = api._manual_sheet_digest(dict(target_id=target_id, is_bom=is_bom, **request))
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
        if selected.keep_stock_quantity > row['max_stock_quantity']:
            raise WarehouseInventoryError('本次加工张数超过已安排与可用库存合计，请刷新核对', 409)
        if selected.keep_stock_quantity > row['remaining_stock_quantity'] and (
                not row['can_increase'] or set(selected.warning_acknowledged_codes) != set(row['warning_codes'])):
            raise WarehouseInventoryError('请核对当前整张加工依据和全部差异提示后再增加用量', 409)
        quantity = row['remaining_stock_quantity'] - selected.keep_stock_quantity
        if quantity:
            changes.append((selected, row, quantity))
    if not changes:
        raise WarehouseInventoryError('本次没有修改采用张数', 409)
    versions = {row['lot_id']: row['version'] for _, row, _ in changes}
    # Release reductions first so a multi-row plan may reuse those same sheets.
    for selected, row, quantity in changes:
        if quantity < 0:
            continue
        # Two reservations can share one physical lot. Validate the whole initial
        # snapshot first, then use the version advanced by this transaction only.
        release_semi_finished_reservation(db, reservation_id=selected.reservation_id,
            expected_version=versions[row['lot_id']], operator_id=user.id, stock_quantity=quantity,
            release_reason='调整未加工片料采用量，差额重新报料',
            idempotency_key='sheet-adjust-' + api._manual_sheet_digest([operation_id, selected.reservation_id])[:60])
        versions[row['lot_id']] += 1
    for selected, row, quantity in changes:
        if quantity > 0:
            continue
        fresh = builder(db, target_id, payload.component_type, user, 'auto')[3]
        reservation = db.get(InventoryReservation, selected.reservation_id, populate_existing=True)
        credit = min(-quantity * int(reservation.yield_factor), fresh['remaining_required_piece_qty'])
        _increase(db, reservation, -quantity, credit, versions[row['lot_id']], user,
            'sheet-increase-' + api._manual_sheet_digest([operation_id, reservation.id])[:60])
        versions[row['lot_id']] += 1
    fresh = builder(db, target_id, payload.component_type, user, 'auto')[3]
    if any(quantity < 0 for _, _, quantity in changes):
        api._revise_manual_internal_demand(db, item, payload.component_type,
            fresh['remaining_required_piece_qty'] + fresh['committed_piece_qty'], user,
            snapshot=snapshot, previous_remaining=preview['remaining_required_piece_qty'] + preview['committed_piece_qty'])
        from app.services.production_workflow import refresh_existing_production_task
        refresh_existing_production_task(db, item_id)
        fresh = builder(db, target_id, payload.component_type, user, 'auto')[3]
    response = dict(released_stock_quantity=sum(max(quantity, 0) for _, _, quantity in changes),
        added_stock_quantity=sum(max(-quantity, 0) for _, _, quantity in changes),
        remaining_required_piece_qty=fresh['remaining_required_piece_qty'],
        semi_finished_reserved_piece_qty=fresh['semi_finished_reserved_piece_qty'],
        committed_piece_qty=fresh['committed_piece_qty'], order_item_id=item_id,
        changes=[dict(reservation_id=selected.reservation_id, before=row['remaining_stock_quantity'],
                      after=selected.keep_stock_quantity) for selected, row, _ in changes],
        message=('本次片料加工安排已更新；请按实际加工结果登记完工' if fresh['remaining_required_piece_qty'] == 0
            else f"片料安排已调整，剩余 {fresh['remaining_required_piece_qty']} 片需补充用料或报料"))
    append_audit_event(db, event_category='business', result='success', source='web',
        module_code='warehouse', action_code=action, resource='semi_finished_inventory', actor=user,
        entity_type='bom_component' if is_bom else 'order_item', entity_id=target_id,
        customer_id=state[2].customer_id, request_id=operation_id,
        description='调整尚未消耗的库存片料采用量', details={'request_digest': digest, 'response': response})
    db.flush()
    return response

"""Transfer only reviewed unused reservations; never reopen consumed history.

Private writer of the administrator cutover transaction. The caller owns the
order lock, reviewed batch versions, idempotency record and final audit/commit.
"""
import hashlib
import json

from sqlalchemy import select, update

from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement
from app.services.multilevel_bom_carried_material import remaining_semi_allocation, semi_source_has_pending_material
from app.services.multilevel_bom_source_handoffs import current_source_handoffs
from app.services.multilevel_bom_plan import BomPlanError, plan_bom


def transfer_semi_remainders(db, *, compiled, reservation_ids, eligible_stock, expected_allocations, actor, operation_key):
    from app.services.semi_finished_inventory import confirm_semi_finished_match, _reservation_status, requirement_signature
    from app.services.warehouse_inventory import _balances, _movement, _number, WarehouseInventoryError
    from app.core.time_contract import utc_now_naive
    if actor is None or not actor.is_active or actor.role != "admin":
        raise BomPlanError("仅活动管理员可转接剩余半成品预占")
    links = {row.source_snapshot_id: row for row in current_source_handoffs(db, compiled)
             if row.source_kind == "manufactured"}
    quantity = compiled.execution_window.execution_quantity
    demand = {row.product_id: row.make_units for row in plan_bom(compiled.graph, quantity,
                                                               eligible_stock=eligible_stock).products}
    nodes = {node.product_id: node for node in compiled.graph.nodes}
    transferred = []
    for reservation_id in sorted(reservation_ids):
        original = db.get(InventoryReservation, reservation_id)
        old = db.get(OrderItemSemiRequirement, original.semi_requirement_id) if original else None
        link = links.get(old.sales_order_item_bom_component_id) if old else None
        if (link is None or original.order_item_id != link.order_item_id or original.reservation_type != "semi_order"
                or semi_source_has_pending_material(db, link.source_snapshot_id)):
            raise BomPlanError("剩余预占缺少明确来源交接或原报料仍待收")
        sheets, pieces = remaining_semi_allocation(original)
        lot = db.get(InventoryLot, original.inventory_lot_id)
        if sheets <= 0 or lot is None or lot.quantity_reserved < sheets:
            raise BomPlanError("待转接半成品批次余额已变化")
        route = next(route for route in nodes[link.product_id].routes if route.key == old.component_type)
        required = demand[link.product_id] * route.pieces_per_unit
        target = db.scalar(select(OrderItemSemiRequirement).where(
            OrderItemSemiRequirement.sales_order_item_bom_component_id == link.target_snapshot_id,
            OrderItemSemiRequirement.component_type == old.component_type))
        if target is None and required:
            fields = {column.key: getattr(old, column.key) for column in old.__table__.columns
                      if column.key not in {"id", "created_at", "updated_at", "created_by", "updated_by"}}
            fields.update(sales_order_item_bom_component_id=link.target_snapshot_id,
                          required_piece_quantity=required, created_by=actor.id)
            target = OrderItemSemiRequirement(**fields)
            db.add(target)
            db.flush()
        already = sum(row.credited_requirement_quantity-row.released_requirement_quantity
            for row in db.scalars(select(InventoryReservation).where(
                InventoryReservation.semi_requirement_id == target.id, InventoryReservation.status != "cancelled"))) if target else 0
        credit = min(pieces, max(required-already, 0))
        take = (credit + original.yield_factor-1)//original.yield_factor
        if expected_allocations.get(original.id) != dict(reserve_sheets=take, credited_pieces=credit,
                                                       released_to_available_sheets=sheets-take):
            raise BomPlanError("半成品转接数量与管理员预览不一致，请重新核对")
        confirmation = None
        if credit:
            if (requirement_signature(target) != requirement_signature(old)
                    or original.yield_factor != target.stock_yield_per_sheet
                    or original.yield_factor != lot.semi_finished_detail.stock_yield_per_sheet):
                raise BomPlanError("半成品转接的原预占、需求或批次张片换算不一致")
            try:
                confirmation = confirm_semi_finished_match(db, requirement_id=target.id,
                    inventory_lot_id=lot.id, operator_id=actor.id, override=False,
                    warning_acknowledged_codes=json.loads(original.warning_codes or "[]"))
            except WarehouseInventoryError as error:
                raise BomPlanError(str(error)) from error
        now = utc_now_naive()
        prefix = hashlib.sha256(f"{operation_key}:semi:{original.id}".encode()).hexdigest()
        before = _balances(lot)
        changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id,
            InventoryLot.version == lot.version, InventoryLot.quantity_reserved >= sheets).values(
                quantity_reserved=InventoryLot.quantity_reserved-sheets,
                quantity_available=InventoryLot.quantity_available+sheets,
                version=InventoryLot.version+1, last_movement_at=now))
        if changed.rowcount != 1:
            raise BomPlanError("半成品释放时批次版本已变化")
        original.released_stock_quantity += sheets
        original.released_requirement_quantity += pieces
        original.released_by, original.released_at = actor.id, now
        original.release_reason = "管理员BOM换版转接未消耗预占，历史消耗保留"
        original.status = _reservation_status(original)
        db.flush()
        db.refresh(lot)
        _movement(db, lot=lot, movement_type="release_reserve", quantity=sheets, before=before,
            operator_id=actor.id, reason=original.release_reason, idempotency_key=prefix+":release",
            reservation_id=original.id, related_order_id=original.order_id, related_order_item_id=original.order_item_id)
        if credit:
            before = _balances(lot)
            lot.quantity_available -= take
            lot.quantity_reserved += take
            lot.version += 1
            new = InventoryReservation(reservation_number=_number("STR"), inventory_lot_id=lot.id,
                reservation_type="semi_order", order_id=original.order_id, order_item_id=original.order_item_id,
                sales_order_item_bom_component_id=link.target_snapshot_id, semi_requirement_id=target.id,
                match_rule_id=confirmation.rule.id, reserved_stock_quantity=take, credited_requirement_quantity=credit,
                yield_factor=original.yield_factor, status="active", warning_codes=original.warning_codes,
                warning_acknowledged_by=actor.id if confirmation.warning_codes else None,
                reserved_by=actor.id, reserved_at=now, idempotency_key=prefix+":reserve")
            db.add(new)
            db.flush()
            _movement(db, lot=lot, movement_type="reserve", quantity=take, before=before,
                operator_id=actor.id, reason="管理员BOM换版承接原剩余预占", idempotency_key=prefix+":reserve",
                reservation_id=new.id, related_order_id=original.order_id, related_order_item_id=original.order_item_id)
            transferred.append(new.id)
    return transferred

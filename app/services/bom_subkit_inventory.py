"""Atomic component-to-subkit conversion over the existing stock ledger."""
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.bom_subkit import OrderSubkit, SubkitConversion, SubkitConversionInput
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.bom_subkit_planning import SubkitMember, plan_receipt_assembly
from app.services.bom_subkits import SubkitError, recipe_rows
from app.services.warehouse_inventory import _balances, _movement, manual_finished_in, inventory_fifo_sort_key


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assemble_subkit_inventory(
    db: Session, *, order_item_id: int, source_lot_versions: dict[int, int],
    target_location_id: int, operation_key: str, operator_id: int,
    available_lot_ids: list[int] | None = None,
) -> SubkitConversion:
    """Use only explicitly supplied, versioned, unreserved component balances.

    Receipt caller holds the outer transaction. No commit or formal-data repair
    is performed here; the savepoint also protects callers that catch failures.
    """
    if not operation_key or len(operation_key) > 100:
        raise SubkitError("组套操作标识无效", 400)
    payload = {"order_item_id": order_item_id, "lots": sorted(source_lot_versions.items()),
               "location_id": target_location_id, "operator_id": operator_id,
               "available_lot_ids": sorted(available_lot_ids) if available_lot_ids is not None else None}
    request_hash = _hash(payload)
    existing = db.scalar(select(SubkitConversion).where(SubkitConversion.idempotency_key == operation_key))
    if existing:
        if existing.request_hash != request_hash or existing.status != "posted":
            raise SubkitError("组套操作标识已使用或该组套已撤销")
        return existing
    with atomic_bom(db):
        item = db.get(OrderItem, order_item_id)
        snapshot = db.get(OrderSubkit, order_item_id)
        if item is None or snapshot is None:
            raise SubkitError("订单缺少冻结的子套件配方")
        order = db.get(Order, item.order_id)
        if item.is_force_closed or int(item.delivered_quantity or 0) >= item.quantity or order.status in ("cancelled", "closed", "已作废", "已结单"):
            raise SubkitError("已结束订单不能继续组套")
        from app.services.production_workflow import lock_order_rows_for_production_transition
        lock_order_rows_for_production_transition(db, [order.id])
        recipe = recipe_rows(snapshot)
        member_ids = {r["product_id"] for r in recipe}
        snapshot_ids = {r["bom_snapshot_id"] for r in recipe}
        from app.services.composite_bom_workflow import _remaining_reservation_quantity, _reservation_status
        reservations = list(db.scalars(select(InventoryReservation).where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.sales_order_item_bom_component_id.in_(snapshot_ids),
            InventoryReservation.status.in_(("active", "partial")),
            InventoryReservation.inventory_lot_id.in_(source_lot_versions)).order_by(InventoryReservation.id)))
        reserved_by_lot = {}
        for reservation in reservations:
            reserved_by_lot.setdefault(reservation.inventory_lot_id, []).append(reservation)
        reserved_qty = {lid: sum(_remaining_reservation_quantity(r) for r in rows)
                        for lid, rows in reserved_by_lot.items()}
        lots = []
        available = {pid: 0 for pid in member_ids}
        free_by_lot = {}
        from app.services.fixed_shelf_staging import staging_owner
        for lot_id, version in source_lot_versions.items():
            lot = db.get(InventoryLot, lot_id)
            if (lot is None or lot.status != "active" or lot.version != version
                    or lot.finished_detail is None or lot.inventory_type != "finished"):
                raise SubkitError("组套原片库存状态或版本已变化")
            detail = lot.finished_detail
            if (detail.product_id not in member_ids or detail.owner_customer_id != order.customer_id
                    or detail.is_general or staging_owner(db, lot.id)):
                raise SubkitError("组套原片产品、客户或集货状态不匹配")
            if reserved_qty.get(lot.id, 0) > lot.quantity_reserved:
                raise SubkitError("原片预占余额不一致")
            free_by_lot[lot.id] = lot.quantity_available if available_lot_ids is None or lot.id in available_lot_ids else 0
            available[detail.product_id] += free_by_lot[lot.id] + reserved_qty.get(lot.id, 0)
            lots.append(lot)
        completed = int(db.scalar(select(func.coalesce(func.sum(
            InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_consumed), 0))
            .join(SubkitConversion, SubkitConversion.id == InventoryLot.source_ref_id).where(
            InventoryLot.source_ref_type == "subkit_conversion",
            SubkitConversion.order_item_id == item.id, SubkitConversion.status == "posted"
        )) or 0)
        plan = plan_receipt_assembly(parent_product_id=item.product_id,
            kit_product_id=snapshot.kit_product_id,
            members=[SubkitMember(r["product_id"], r["pieces_per_kit"]) for r in recipe],
            remaining_kit_demand=max(item.quantity * snapshot.kits_per_parent - completed, 0),
            eligible_pieces=available)
        conversion = SubkitConversion(order_item_id=item.id, idempotency_key=operation_key,
            request_hash=request_hash, quantity=plan.kit_quantity, total_cost=Decimal(0),
            status="posted", created_by=operator_id)
        db.add(conversion)
        db.flush()
        to_consume = {row.product_id: row.consumed_pieces for row in plan.components}
        cost = Decimal(0)
        cost_sources = []
        for lot in sorted(lots, key=inventory_fifo_sort_key):
            pid = lot.finished_detail.product_id
            take = min(free_by_lot[lot.id] + reserved_qty.get(lot.id, 0), to_consume[pid])
            if not take:
                continue
            reserved_take = min(take, reserved_qty.get(lot.id, 0))
            available_take = take - reserved_take
            from app.services.bom_subkit_costs import source_cost
            part_cost, lineage = source_cost(db, lot, take)
            cost_sources.append({**lineage, "lot_id": lot.id, "quantity": take, "cost": str(part_cost)})
            currencies = {r["currency"] for r in cost_sources if r["currency"]}
            if len(currencies) > 1:
                raise SubkitError("原片采购币种不同，请先确认换算成本，不能直接合并")
            before = _balances(lot)
            changed = db.execute(update(InventoryLot).where(
                InventoryLot.id == lot.id, InventoryLot.version == source_lot_versions[lot.id],
                InventoryLot.quantity_available >= available_take,
                InventoryLot.quantity_reserved >= reserved_take,
            ).values(quantity_available=InventoryLot.quantity_available - available_take,
                     quantity_reserved=InventoryLot.quantity_reserved - reserved_take,
                     quantity_consumed=InventoryLot.quantity_consumed + take,
                     version=InventoryLot.version + 1))
            if changed.rowcount != 1:
                raise SubkitError("组套原片已被其他操作使用")
            db.refresh(lot)
            movement = _movement(db, lot=lot, movement_type="consume", quantity=take,
                before=before, operator_id=operator_id, reason="原片自动组成子套件",
                idempotency_key=f"{operation_key}:in:{lot.id}", related_order_item_id=item.id)
            db.flush()
            used_reservations = []
            for reservation in reserved_by_lot.get(lot.id, []):
                debit = min(reserved_take, _remaining_reservation_quantity(reservation))
                if debit:
                    reservation.consumed_stock_quantity += debit
                    reservation.consumed_requirement_quantity += debit
                    reservation.consumed_by = operator_id
                    reservation.consumed_at = utc_now_naive()
                    reservation.status = _reservation_status(reservation)
                    used_reservations.append({"id": reservation.id, "quantity": debit})
                    reserved_take -= debit
            db.add(SubkitConversionInput(conversion_id=conversion.id, lot_id=lot.id,
                product_id=pid, quantity=take, total_cost=part_cost, consume_movement_id=movement.id,
                reservations_json=json.dumps(used_reservations)))
            to_consume[pid] -= take
            cost += part_cost
        if any(to_consume.values()):
            raise SubkitError("组套原片扣减不完整")
        if plan.kit_quantity:
            destination = db.get(WarehouseLocation, target_location_id)
            if destination is None:
                raise SubkitError("组套目标库位不存在")
            output = manual_finished_in(db, customer_id=order.customer_id,
                product_id=snapshot.kit_product_id, location_id=target_location_id,
                quantity=plan.kit_quantity, stock_date=beijing_today(), source_type="transfer",
                source_ref_type="subkit_conversion", source_ref_id=conversion.id,
                expected_layout_version=destination.floor3_layout.version if destination.floor3_layout else None,
                remarks="收料自动组套", operator_id=operator_id,
                idempotency_key=f"{operation_key}:out", movement_reason="原片自动组套入库")
            output.finished_detail.product_name_snapshot = snapshot.kit_name_snapshot
            output.estimated_unit_cost_snapshot = (cost / plan.kit_quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            output.cost_snapshot_source = "subkit_conversion"
            output.cost_snapshot_detail_json = json.dumps({"conversion_id": conversion.id, "total_cost": str(cost), "quantity": plan.kit_quantity})
            output.cost_snapshot_at = utc_now_naive()
            conversion.output_lot_id = output.id
        conversion.total_cost = cost
        conversion.cost_detail_json = json.dumps({"sources": cost_sources,
            "actual": bool(cost_sources) and all(r["actual"] for r in cost_sources),
            "currency": next(iter({r["currency"] for r in cost_sources if r["currency"]}), "")})
        append_audit_event(db, event_category="business", result="success", source="system",
            module_code="warehouse", action_code="assemble_subkit", resource="subkit_conversion",
            actor=db.get(User, operator_id), entity_type="subkit_conversion", entity_id=conversion.id,
            customer_id=order.customer_id, details={"quantity": plan.kit_quantity, "cost": str(cost), "source_lot_ids": sorted(source_lot_versions)})
        db.flush()
        return conversion


def reverse_subkit_conversion(db: Session, *, conversion_id: int, operator_id: int) -> None:
    with atomic_bom(db):
        conversion = db.get(SubkitConversion, conversion_id)
        if conversion is None:
            raise SubkitError("组套记录不存在", 404)
        if conversion.status == "reversed":
            return
        if conversion.output_lot_id:
            output = db.get(InventoryLot, conversion.output_lot_id)
            if (output is None or output.quantity_available != conversion.quantity or output.quantity_reserved
                    or output.quantity_consumed or output.quantity_damaged or output.quantity_scrapped
                    or output.version != 1):
                raise SubkitError("子套件已盘点、移库、预占或送货，不能撤销组套")
            before = _balances(output)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == output.id, InventoryLot.version == 1).values(
                quantity_available=0, quantity_consumed=conversion.quantity, version=2, status="closed"))
            if changed.rowcount != 1:
                raise SubkitError("子套件库存已变化")
            db.refresh(output)
            _movement(db, lot=output, movement_type="consume", quantity=conversion.quantity, before=before,
                operator_id=operator_id, reason="撤销组套成品", idempotency_key=f"subkit-reverse:{conversion.id}:out")
        inputs = list(db.scalars(select(SubkitConversionInput).where(SubkitConversionInput.conversion_id == conversion.id)))
        for source in inputs:
            lot = db.get(InventoryLot, source.lot_id)
            if lot is None or lot.quantity_consumed < source.quantity:
                raise SubkitError("组套原片消耗记录不完整")
            before = _balances(lot)
            version = lot.version
            restored_reservations = json.loads(source.reservations_json or "[]")
            reserved_restore = sum(r["quantity"] for r in restored_reservations)
            from app.services.composite_bom_workflow import _reservation_status
            for record in restored_reservations:
                reservation = db.get(InventoryReservation, record["id"])
                if reservation is None or reservation.consumed_stock_quantity < record["quantity"]:
                    raise SubkitError("原片预占消耗来源不完整")
                reservation.consumed_stock_quantity -= record["quantity"]
                reservation.consumed_requirement_quantity -= record["quantity"]
                reservation.status = _reservation_status(reservation)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id, InventoryLot.version == version).values(
                quantity_available=InventoryLot.quantity_available + source.quantity - reserved_restore,
                quantity_reserved=InventoryLot.quantity_reserved + reserved_restore,
                quantity_consumed=InventoryLot.quantity_consumed - source.quantity, version=InventoryLot.version + 1))
            if changed.rowcount != 1:
                raise SubkitError("原片库存已变化")
            db.refresh(lot)
            _movement(db, lot=lot, movement_type="reverse_consume", quantity=source.quantity, before=before,
                operator_id=operator_id, reason="撤销组套恢复原片", reversal_of_movement_id=source.consume_movement_id,
                idempotency_key=f"subkit-reverse:{conversion.id}:{source.id}")
        conversion.status = "reversed"
        append_audit_event(db, event_category="business", result="success", source="system", module_code="warehouse",
            action_code="reverse_subkit", resource="subkit_conversion", actor=db.get(User, operator_id),
            entity_type="subkit_conversion", entity_id=conversion.id, details={"quantity": conversion.quantity})
        db.flush()

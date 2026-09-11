from __future__ import annotations

from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.time_contract import utc_now_naive
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryReservation,
    OrderedFinishedReceiptReturn,
    WarehouseLocation,
)
from app.services.location_candidates import (
    claim_active_placed_location,
    has_space_ledger,
    list_operational_locations,
    operational_location_issue,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    _balances,
    _ensure_finished_projection_postcondition,
    _finished_reservation_status,
    _movement,
    _number,
    reverse_finished_consumption,
)


def active_ordered_return_location_ids(
    db: Session,
    *,
    return_receipt_item_ids: list[int],
) -> dict[int, int]:
    if not return_receipt_item_ids:
        return {}
    rows = db.execute(
        select(
            OrderedFinishedReceiptReturn.return_receipt_item_id,
            OrderedFinishedReceiptReturn.return_location_id,
            OrderedFinishedReceiptReturn.sequence_no,
        ).where(
            OrderedFinishedReceiptReturn.return_receipt_item_id.in_(
                return_receipt_item_ids
            )
        ).order_by(
            OrderedFinishedReceiptReturn.return_receipt_item_id,
            OrderedFinishedReceiptReturn.sequence_no.desc(),
        )
    ).all()
    result: dict[int, int] = {}
    for receipt_item_id, location_id, _sequence_no in rows:
        result.setdefault(int(receipt_item_id), int(location_id))
    return result


def _source_allocation_rows(
    db: Session,
    *,
    delivery_item: DeliveryItem,
) -> list[tuple[DeliveryInventoryAllocation, InventoryReservation, InventoryLot]]:
    rows = list(
        db.execute(
            select(
                DeliveryInventoryAllocation,
                InventoryReservation,
                InventoryLot,
            )
            .join(
                InventoryReservation,
                InventoryReservation.id
                == DeliveryInventoryAllocation.reservation_id,
            )
            .join(
                InventoryLot,
                InventoryLot.id == InventoryReservation.inventory_lot_id,
            )
            .where(
                DeliveryInventoryAllocation.delivery_item_id == delivery_item.id,
                InventoryReservation.sales_order_item_bom_component_id.is_(None),
            )
            .order_by(DeliveryInventoryAllocation.id.desc())
        ).all()
    )
    for allocation, reservation, lot in rows:
        if (
            delivery_item.order_item_id is None
            or reservation.order_item_id != delivery_item.order_item_id
            or lot.inventory_type != "finished"
            or lot.finished_detail is None
        ):
            raise WarehouseInventoryError("送货库存分配与订单明细不一致", 409)
        available = int(allocation.consumed_stock_quantity or 0) - int(
            allocation.reversed_stock_quantity or 0
        )
        if available < 0:
            raise WarehouseInventoryError("送货库存分配冲回数量异常", 409)
    return rows


def _next_sequence(db: Session, return_receipt_item_id: int) -> int:
    return int(
        db.scalar(
            select(func.max(OrderedFinishedReceiptReturn.sequence_no)).where(
                OrderedFinishedReceiptReturn.return_receipt_item_id
                == return_receipt_item_id
            )
        )
        or 0
    ) + 1


def _claim_return_destination(
    db: Session,
    location_id: int,
    *,
    expected_layout_version: int | None = None,
) -> None:
    try:
        claimed = claim_active_placed_location(
            db,
            location_id,
            expected_layout_version=expected_layout_version,
        )
    except OperationalError as error:
        raise WarehouseInventoryError(
            "退回库位正在被其他入库、移位或布局操作使用，请稍后重试", 409
        ) from error
    if not claimed:
        raise WarehouseInventoryError(
            "退回库位已停用、尚未落位或状态已变化，请刷新后重试", 409
        )


def _return_location_candidates(db: Session) -> dict[int, WarehouseLocation]:
    """Return empty destinations that pass the current authoritative map gate."""

    rows = list_operational_locations(
        db,
        warehouse_types={"finished", "shared"},
        empty_only=True,
    )
    if not has_space_ledger(db):
        return {int(row.location.id): row.location for row in rows}

    occupied_pallet_counts = {
        (int(floor_number), str(area_code or "").strip().upper()): int(count)
        for floor_number, area_code, count in db.execute(
            select(
                WarehouseLocation.warehouse_floor,
                func.upper(WarehouseLocation.area_code),
                func.count(InventoryPallet.id),
            )
            .join(
                InventoryPallet,
                InventoryPallet.location_id == WarehouseLocation.id,
            )
            .where(InventoryPallet.is_current.is_(True))
            .group_by(
                WarehouseLocation.warehouse_floor,
                func.upper(WarehouseLocation.area_code),
            )
        ).all()
        if floor_number is not None
    }
    return {
        int(row.location.id): row.location
        for row in rows
        if operational_location_issue(
            db,
            row.location,
            warehouse_types={"finished", "shared"},
            require_published=True,
            require_map_geometry=True,
            required_inventory_type="finished",
            require_empty=True,
            projection_context=row.projection_context,
            known_occupied=row.occupied,
            area_occupied_pallet_count=occupied_pallet_counts.get(
                (
                    int(row.location.warehouse_floor or 0),
                    str(row.location.area_code or "").strip().upper(),
                ),
                0,
            ),
        )
        is None
    }


def _clone_return_lot(
    db: Session,
    *,
    source_lot: InventoryLot,
    return_receipt_item_id: int,
    delivery_item: DeliveryItem,
    delivery: Delivery,
    location_id: int,
    quantity: int,
    resolution_action: str,
    stock_date: date,
    operator_id: int | None,
    sequence_no: int,
    pallet_id: int | None,
) -> tuple[InventoryLot, InventoryReservation | None, InventoryMovement, int | None]:
    source_detail = source_lot.finished_detail
    if source_detail is None:
        raise WarehouseInventoryError("送货来源成品批次资料不完整", 409)
    now = utc_now_naive()
    reserved = quantity if resolution_action == "continue_delivery" else 0
    available = quantity if resolution_action == "accept_short" else 0
    lot = InventoryLot(
        lot_number=_number("FG-RETURN"),
        inventory_type="finished",
        warehouse_location_id=location_id,
        quantity_available=available,
        quantity_reserved=reserved,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit=source_lot.unit,
        status="active",
        source_type="delivery_return",
        source_ref_type="return_receipt_item",
        source_ref_id=return_receipt_item_id,
        stock_date=stock_date,
        stock_date_accuracy="exact",
        stock_date_original_text=stock_date.isoformat(),
        last_movement_at=now,
        version=1,
        remarks=f"客户短收退回，原批次 {source_lot.lot_number}",
        estimated_unit_cost_snapshot=source_lot.estimated_unit_cost_snapshot,
        estimated_square_price_snapshot=source_lot.estimated_square_price_snapshot,
        estimated_cost_area_m2_snapshot=source_lot.estimated_cost_area_m2_snapshot,
        cost_snapshot_source=source_lot.cost_snapshot_source,
        cost_snapshot_detail_json=source_lot.cost_snapshot_detail_json,
        cost_snapshot_at=source_lot.cost_snapshot_at,
        created_by=operator_id,
    )
    lot.finished_detail = FinishedGoodsInventoryDetail(
        owner_customer_id=source_detail.owner_customer_id,
        owner_customer_name_snapshot=source_detail.owner_customer_name_snapshot,
        is_general=source_detail.is_general,
        product_id=source_detail.product_id,
        inventory_code_snapshot=source_detail.inventory_code_snapshot,
        product_name_snapshot=source_detail.product_name_snapshot,
        box_type_snapshot=source_detail.box_type_snapshot,
        length_mm=source_detail.length_mm,
        width_mm=source_detail.width_mm,
        height_mm=source_detail.height_mm,
        material_code_snapshot=source_detail.material_code_snapshot,
        flute_type_snapshot=source_detail.flute_type_snapshot,
        physical_basis_json=source_detail.physical_basis_json,
    )
    db.add(lot)
    db.flush()

    reservation: InventoryReservation | None = None
    if resolution_action == "continue_delivery":
        order_item = db.get(OrderItem, delivery_item.order_item_id)
        order = db.get(Order, order_item.order_id) if order_item is not None else None
        if order_item is None or order is None:
            raise WarehouseInventoryError("短收退回关联订单不存在", 409)
        reservation = InventoryReservation(
            reservation_number=_number("RS-RETURN"),
            inventory_lot_id=lot.id,
            reservation_type="finished_order",
            order_id=order.id,
            order_item_id=order_item.id,
            reserved_stock_quantity=quantity,
            credited_requirement_quantity=quantity,
            yield_factor=1,
            consumed_stock_quantity=0,
            released_stock_quantity=0,
            consumed_requirement_quantity=0,
            released_requirement_quantity=0,
            status="active",
            warning_codes="[]",
            reserved_by=operator_id,
            reserved_at=now,
            reservation_group_key=(
                f"receipt-return-{return_receipt_item_id}-{sequence_no}"
            ),
            reservation_group_requested_quantity=quantity,
            idempotency_key=(
                f"ordered-receipt-return-reserve-{return_receipt_item_id}-{sequence_no}"
            ),
        )
        db.add(reservation)
        db.flush()

    movement = _movement(
        db,
        lot=lot,
        movement_type="return_in",
        quantity=quantity,
        before={key: 0 for key in _balances(lot)},
        operator_id=operator_id,
        reason="客户短收退回成品入库",
        remarks=(
            "继续待送，退回数量已保留给原订单"
            if resolution_action == "continue_delivery"
            else "按实收结单，退回数量转为客户专用可用库存"
        ),
        idempotency_key=(
            f"ordered-receipt-return-in-{return_receipt_item_id}-{sequence_no}"
        ),
        reservation_id=reservation.id if reservation is not None else None,
        related_order_id=(reservation.order_id if reservation is not None else None),
        related_order_item_id=delivery_item.order_item_id,
        related_delivery_id=delivery.id,
    )
    db.flush()

    pallet = _ensure_finished_projection_postcondition(
        db,
        lot=lot,
        operator_id=operator_id,
        create_missing=True,
        pallet_id=pallet_id,
        require_empty_pallet=pallet_id is None,
    )
    if pallet is not None:
        pallet_id = int(pallet.id)
    return lot, reservation, movement, pallet_id


def _reverse_and_relocate_source_quantity(
    db: Session,
    *,
    allocation: DeliveryInventoryAllocation,
    source_reservation: InventoryReservation,
    source_lot: InventoryLot,
    quantity: int,
    return_receipt_item_id: int,
    sequence_no: int,
    delivery: Delivery,
    operator_id: int | None,
) -> tuple[InventoryLot, InventoryMovement, InventoryMovement]:
    mutation = reverse_finished_consumption(
        db,
        reservation_id=source_reservation.id,
        stock_quantity=quantity,
        expected_version=int(source_lot.version),
        operator_id=operator_id,
        idempotency_key=(
            f"ordered-receipt-source-reverse-{return_receipt_item_id}-{sequence_no}"
        ),
        allocation_id=allocation.id,
    )
    source_lot = db.get(InventoryLot, source_lot.id)
    source_reservation = db.get(InventoryReservation, source_reservation.id)
    if source_lot is None or source_reservation is None:
        raise WarehouseInventoryError("客户短收原发货库存不存在", 409)
    before = _balances(source_lot)
    now = utc_now_naive()
    version = int(source_lot.version)
    changed = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == source_lot.id,
            InventoryLot.version == version,
            InventoryLot.quantity_reserved >= quantity,
        )
        .values(
            quantity_reserved=InventoryLot.quantity_reserved - quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise WarehouseInventoryError("客户短收原发货库存已变化，请刷新后重试", 409)
    source_reservation.released_stock_quantity += quantity
    source_reservation.released_requirement_quantity += quantity
    source_reservation.released_by = operator_id
    source_reservation.released_at = now
    source_reservation.release_reason = "客户短收退回转入实际库位"
    source_reservation.status = _finished_reservation_status(source_reservation)
    db.flush()
    db.expire(source_lot)
    source_lot = db.get(InventoryLot, source_lot.id)
    assert source_lot is not None
    transfer_movement = _movement(
        db,
        lot=source_lot,
        movement_type="location_transfer",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason="客户短收退回转入实际库位",
        remarks=f"回单明细 {return_receipt_item_id}",
        idempotency_key=(
            f"ordered-receipt-source-transfer-{return_receipt_item_id}-{sequence_no}"
        ),
        reservation_id=source_reservation.id,
        related_order_id=source_reservation.order_id,
        related_order_item_id=source_reservation.order_item_id,
        related_delivery_id=delivery.id,
        reversal_of_movement_id=mutation.movement.id,
    )
    db.flush()
    return source_lot, mutation.movement, transfer_movement


def restore_ordered_finished_receipt_shortage(
    db: Session,
    *,
    delivery: Delivery,
    delivery_item: DeliveryItem,
    return_receipt_item_id: int,
    actual_received_quantity: int,
    resolution_action: str | None,
    return_location_id: int | None,
    expected_return_layout_version: int | None = None,
    stock_date: date,
    operator_id: int | None,
) -> list[OrderedFinishedReceiptReturn]:
    shortage = int(delivery_item.delivered_quantity or 0) - int(
        actual_received_quantity or 0
    )
    if shortage <= 0 or delivery_item.source_type == "unordered_finished":
        return []
    allocations = _source_allocation_rows(db, delivery_item=delivery_item)
    if not allocations:
        # Historical quantity-only deliveries have no formal stock fact to
        # return.  Keep them editable without inventing inventory.
        return []
    if resolution_action not in {"continue_delivery", "accept_short"}:
        raise WarehouseInventoryError("客户短收处理方式无效", 409)
    if return_location_id is None:
        raise WarehouseInventoryError("客户短收退回请先选择实际存放库位", 400)
    _claim_return_destination(
        db,
        return_location_id,
        expected_layout_version=expected_return_layout_version,
    )
    # The destination claim serializes this fresh projection read with map and
    # inventory writers.  Historical/unmapped locations remain valid sources,
    # but a customer return must never create new stock outside the current map.
    allowed_locations = _return_location_candidates(db)
    if return_location_id not in allowed_locations:
        raise WarehouseInventoryError("退回库位不可用或已有货物，请重新选择", 409)

    existing = list(
        db.scalars(
            select(OrderedFinishedReceiptReturn).where(
                OrderedFinishedReceiptReturn.return_receipt_item_id
                == return_receipt_item_id,
                OrderedFinishedReceiptReturn.status == "active",
            )
        ).all()
    )
    if existing:
        if (
            sum(int(row.quantity) for row in existing) != shortage
            or any(row.return_location_id != return_location_id for row in existing)
            or any(row.resolution_action != resolution_action for row in existing)
        ):
            raise WarehouseInventoryError("该回单短收退回已经按其他内容入账", 409)
        return existing

    available_total = sum(
        int(allocation.consumed_stock_quantity or 0)
        - int(allocation.reversed_stock_quantity or 0)
        for allocation, _reservation, _lot in allocations
    )
    if shortage > available_total:
        raise WarehouseInventoryError("客户短收数量超过本次正式出库数量", 409)

    remaining = shortage
    sequence_no = _next_sequence(db, return_receipt_item_id)
    pallet_id: int | None = None
    created: list[OrderedFinishedReceiptReturn] = []
    for allocation, source_reservation, source_lot in allocations:
        allocation_available = int(allocation.consumed_stock_quantity or 0) - int(
            allocation.reversed_stock_quantity or 0
        )
        quantity = min(remaining, allocation_available)
        if quantity <= 0:
            continue
        source_lot, source_reverse_movement, source_transfer_movement = (
            _reverse_and_relocate_source_quantity(
                db,
                allocation=allocation,
                source_reservation=source_reservation,
                source_lot=source_lot,
                quantity=quantity,
                return_receipt_item_id=return_receipt_item_id,
                sequence_no=sequence_no,
                delivery=delivery,
                operator_id=operator_id,
            )
        )
        lot, reservation, movement, pallet_id = _clone_return_lot(
            db,
            source_lot=source_lot,
            return_receipt_item_id=return_receipt_item_id,
            delivery_item=delivery_item,
            delivery=delivery,
            location_id=return_location_id,
            quantity=quantity,
            resolution_action=resolution_action,
            stock_date=stock_date,
            operator_id=operator_id,
            sequence_no=sequence_no,
            pallet_id=pallet_id,
        )
        fact = OrderedFinishedReceiptReturn(
            return_receipt_item_id=return_receipt_item_id,
            sequence_no=sequence_no,
            delivery_item_id=delivery_item.id,
            delivery_inventory_allocation_id=allocation.id,
            source_inventory_lot_id=source_lot.id,
            return_inventory_lot_id=lot.id,
            return_location_id=return_location_id,
            reservation_id=reservation.id if reservation is not None else None,
            return_in_movement_id=movement.id,
            source_reverse_movement_id=source_reverse_movement.id,
            source_transfer_movement_id=source_transfer_movement.id,
            quantity=quantity,
            resolution_action=resolution_action,
            status="active",
            idempotency_key=(
                f"ordered-receipt-return-{return_receipt_item_id}-{sequence_no}"
            ),
            created_by=operator_id,
        )
        db.add(fact)
        db.flush()
        from app.services.bom_return_cost import freeze_return_graph_cost
        from app.services.bom_subkits import SubkitError
        try:
            freeze_return_graph_cost(db, returned=fact, lot=lot, allocation=allocation)
        except SubkitError as error:
            raise WarehouseInventoryError(str(error), 409) from error
        created.append(fact)
        remaining -= quantity
        sequence_no += 1
        if remaining == 0:
            break
    if remaining:
        raise WarehouseInventoryError("客户短收数量未能完整对应本次正式出库", 409)
    db.flush()
    return created


def reconsume_ordered_finished_receipt_returns(
    db: Session,
    *,
    return_receipt_item_ids: list[int],
    operator_id: int | None,
) -> None:
    if not return_receipt_item_ids:
        return
    facts = list(
        db.scalars(
            select(OrderedFinishedReceiptReturn)
            .where(
                OrderedFinishedReceiptReturn.return_receipt_item_id.in_(
                    return_receipt_item_ids
                ),
                OrderedFinishedReceiptReturn.status == "active",
            )
            .order_by(OrderedFinishedReceiptReturn.id)
        ).all()
    )
    affected_pallet_ids: set[int] = set()
    for fact in facts:
        lot = db.get(InventoryLot, fact.return_inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError("客户短收退回批次不存在", 409)
        quantity = int(fact.quantity)
        reservation = (
            db.get(InventoryReservation, fact.reservation_id)
            if fact.reservation_id is not None
            else None
        )
        source_allocation = db.get(
            DeliveryInventoryAllocation,
            fact.delivery_inventory_allocation_id,
        )
        source_lot = db.get(InventoryLot, fact.source_inventory_lot_id)
        source_reservation = (
            db.get(InventoryReservation, source_allocation.reservation_id)
            if source_allocation is not None
            else None
        )
        if (
            source_allocation is None
            or source_lot is None
            or source_reservation is None
            or int(source_allocation.reversed_stock_quantity or 0) < quantity
            or int(source_allocation.reversed_requirement_quantity or 0) < quantity
            or int(source_reservation.released_stock_quantity or 0) < quantity
            or int(source_reservation.released_requirement_quantity or 0) < quantity
        ):
            raise WarehouseInventoryError("客户短收原发货冲回记录不完整", 409)
        untouched = (
            lot.status == "active"
            and int(lot.warehouse_location_id) == int(fact.return_location_id)
            and int(lot.version or 0) == 1
            and int(lot.quantity_consumed or 0) == 0
            and int(lot.quantity_damaged or 0) == 0
            and int(lot.quantity_scrapped or 0) == 0
        )
        if fact.resolution_action == "continue_delivery":
            untouched = untouched and (
                int(lot.quantity_available or 0) == 0
                and int(lot.quantity_reserved or 0) == quantity
                and reservation is not None
                and reservation.status == "active"
                and int(reservation.consumed_stock_quantity or 0) == 0
                and int(reservation.released_stock_quantity or 0) == 0
            )
        else:
            untouched = untouched and (
                int(lot.quantity_available or 0) == quantity
                and int(lot.quantity_reserved or 0) == 0
                and reservation is None
            )
        later_movement = db.scalar(
            select(InventoryMovement.id)
            .where(
                InventoryMovement.inventory_lot_id == lot.id,
                InventoryMovement.id != fact.return_in_movement_id,
            )
            .limit(1)
        )
        if not untouched or later_movement is not None:
            raise WarehouseInventoryError(
                f"退回批次 {lot.lot_number} 已被移动或使用，不能修改或取消原回单",
                409,
            )
        if lot.pallet_item is not None:
            affected_pallet_ids.add(int(lot.pallet_item.pallet_id))
        before = _balances(lot)
        now = utc_now_naive()
        values = {
            "quantity_consumed": InventoryLot.quantity_consumed + quantity,
            "version": InventoryLot.version + 1,
            "last_movement_at": now,
            "status": "closed",
        }
        if fact.resolution_action == "continue_delivery":
            values["quantity_reserved"] = InventoryLot.quantity_reserved - quantity
        else:
            values["quantity_available"] = InventoryLot.quantity_available - quantity
        changed = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == 1,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise WarehouseInventoryError("退回库存已变化，请刷新后重试", 409)
        if reservation is not None:
            reservation.released_stock_quantity = quantity
            reservation.released_requirement_quantity = quantity
            reservation.released_by = operator_id
            reservation.released_at = now
            reservation.release_reason = "撤销或修改客户短收回单"
            reservation.status = "released"
        db.flush()
        db.expire(lot)
        lot = db.get(InventoryLot, lot.id)
        assert lot is not None
        movement = _movement(
            db,
            lot=lot,
            movement_type="return_reconsume",
            quantity=quantity,
            before=before,
            operator_id=operator_id,
            reason="撤销或修改客户短收回单",
            remarks=f"回单明细 {fact.return_receipt_item_id}",
            idempotency_key=f"ordered-receipt-return-reconsume-{fact.id}",
            reservation_id=reservation.id if reservation is not None else None,
            related_order_id=reservation.order_id if reservation is not None else None,
            related_order_item_id=(
                reservation.order_item_id
                if reservation is not None
                else db.get(DeliveryItem, fact.delivery_item_id).order_item_id
            ),
            related_delivery_id=db.get(DeliveryItem, fact.delivery_item_id).delivery_id,
            reversal_of_movement_id=fact.return_in_movement_id,
        )
        db.flush()
        source_before = _balances(source_lot)
        source_version = int(source_lot.version)
        source_changed = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == source_lot.id,
                InventoryLot.version == source_version,
            )
            .values(
                quantity_consumed=InventoryLot.quantity_consumed + quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if source_changed.rowcount != 1:
            raise WarehouseInventoryError("客户短收原发货库存已变化，请刷新后重试", 409)
        source_reservation.consumed_stock_quantity += quantity
        source_reservation.consumed_requirement_quantity += quantity
        source_reservation.released_stock_quantity -= quantity
        source_reservation.released_requirement_quantity -= quantity
        source_reservation.release_reason = None
        if int(source_reservation.released_stock_quantity or 0) == 0:
            source_reservation.released_by = None
            source_reservation.released_at = None
        source_reservation.status = _finished_reservation_status(source_reservation)
        source_allocation.reversed_stock_quantity -= quantity
        source_allocation.reversed_requirement_quantity -= quantity
        source_allocation.status = (
            "active"
            if int(source_allocation.reversed_stock_quantity or 0) == 0
            else "partial"
        )
        if source_allocation.status == "active":
            source_allocation.reversed_by = None
            source_allocation.reversed_at = None
        db.flush()
        db.expire(source_lot)
        source_lot = db.get(InventoryLot, source_lot.id)
        assert source_lot is not None
        source_reconsume_movement = _movement(
            db,
            lot=source_lot,
            movement_type="return_reconsume",
            quantity=quantity,
            before=source_before,
            operator_id=operator_id,
            reason="撤销或修改客户短收回单，恢复原发货消耗",
            remarks=f"回单明细 {fact.return_receipt_item_id}",
            idempotency_key=f"ordered-receipt-source-reconsume-{fact.id}",
            reservation_id=source_reservation.id,
            related_order_id=source_reservation.order_id,
            related_order_item_id=source_reservation.order_item_id,
            related_delivery_id=db.get(DeliveryItem, fact.delivery_item_id).delivery_id,
            reversal_of_movement_id=fact.source_reverse_movement_id,
        )
        db.flush()
        fact.status = "reconsumed"
        fact.reconsume_movement_id = movement.id
        fact.source_reconsume_movement_id = source_reconsume_movement.id
        fact.reconsumed_by = operator_id
        fact.reconsumed_at = now
    if affected_pallet_ids:
        from app.models.warehouse_inventory import InventoryPallet
        from app.services.floor3_locations import Floor3LocationError, clear_pallet
        from app.services.warehouse_ground_slots import (
            release_ground_occupancy_for_pallet,
        )

        for pallet_id in sorted(affected_pallet_ids):
            pallet = db.get(InventoryPallet, pallet_id)
            if pallet is None or not pallet.is_current or pallet.location_id is None:
                continue
            try:
                clear_pallet(
                    db,
                    pallet_id=pallet.id,
                    expected_version=int(pallet.version),
                    remarks="撤销或修改客户短收回单，自动清空退回栈板",
                    operator_id=operator_id,
                )
                release_ground_occupancy_for_pallet(
                    db,
                    pallet_id=int(pallet.id),
                    operator_id=operator_id,
                )
            except Floor3LocationError as error:
                raise WarehouseInventoryError(str(error), error.status_code) from error
    db.flush()

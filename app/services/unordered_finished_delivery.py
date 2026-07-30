from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.time_contract import utc_now_naive
from app.models.delivery import Delivery, DeliveryItem
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    UnorderedFinishedDeliveryAllocation,
    UnorderedFinishedDeliveryReversal,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    _balances,
    _movement,
)


def _validated_customer_lot(
    db: Session,
    *,
    delivery: Delivery,
    delivery_item: DeliveryItem,
    allocation: UnorderedFinishedDeliveryAllocation,
) -> InventoryLot:
    lot = db.get(InventoryLot, allocation.inventory_lot_id)
    detail = db.get(FinishedGoodsInventoryDetail, allocation.inventory_lot_id)
    if lot is None or detail is None:
        raise WarehouseInventoryError("所选成品库存批次不存在", 409)
    if (
        lot.inventory_type != "finished"
        or detail.is_general
        or detail.owner_customer_id != delivery.customer_id
        or detail.product_id != delivery_item.product_id
    ):
        raise WarehouseInventoryError(
            "所选库存已不属于当前客户或当前产品，请刷新后重试",
            409,
        )
    return lot


def dispatch_unordered_finished_inventory(
    db: Session,
    *,
    delivery: Delivery,
    delivery_items: list[DeliveryItem],
    operator_id: int | None,
    dispatched_at: datetime,
) -> None:
    """Post exact draft lots without creating an order reservation."""

    for delivery_item in delivery_items:
        if delivery_item.source_type != "unordered_finished":
            raise WarehouseInventoryError("送货单来源不一致，禁止混合发货", 409)
        allocations = db.scalars(
            select(UnorderedFinishedDeliveryAllocation)
            .where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id
                == delivery_item.id
            )
            .order_by(UnorderedFinishedDeliveryAllocation.id)
        ).all()
        if not allocations or sum(
            int(row.planned_quantity or 0) for row in allocations
        ) != int(delivery_item.delivered_quantity or 0):
            raise WarehouseInventoryError("无订单成品库存分配与送货数量不一致", 409)
        for allocation in allocations:
            if (
                allocation.status != "planned"
                or int(allocation.consumed_quantity or 0) != 0
                or int(allocation.restored_quantity or 0) != 0
                or allocation.consume_movement_id is not None
            ):
                raise WarehouseInventoryError("该库存批次已经处理，请刷新后重试", 409)
            lot = _validated_customer_lot(
                db,
                delivery=delivery,
                delivery_item=delivery_item,
                allocation=allocation,
            )
            quantity = int(allocation.planned_quantity)
            if (
                lot.status != "active"
                or int(lot.quantity_reserved or 0) != 0
                or int(lot.quantity_available or 0) < quantity
            ):
                raise WarehouseInventoryError(
                    f"批次 {lot.lot_number} 可用库存已变化，请刷新后重试",
                    409,
                )
            before = _balances(lot)
            version = int(lot.version)
            changed = db.execute(
                update(InventoryLot)
                .where(
                    InventoryLot.id == lot.id,
                    InventoryLot.version == version,
                    InventoryLot.status == "active",
                    InventoryLot.quantity_reserved == 0,
                    InventoryLot.quantity_available >= quantity,
                )
                .values(
                    quantity_available=InventoryLot.quantity_available - quantity,
                    quantity_consumed=InventoryLot.quantity_consumed + quantity,
                    version=InventoryLot.version + 1,
                    last_movement_at=dispatched_at,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise WarehouseInventoryError(
                    f"批次 {lot.lot_number} 库存已变化，请刷新后重试",
                    409,
                )
            db.flush()
            db.expire(lot)
            lot = db.get(InventoryLot, lot.id)
            assert lot is not None
            movement = _movement(
                db,
                lot=lot,
                movement_type="consume",
                quantity=quantity,
                before=before,
                operator_id=operator_id,
                reason="无订单客户专用成品正式送货出库",
                remarks=f"送货明细 {delivery_item.id}",
                idempotency_key=(
                    f"unordered-finished-dispatch-{delivery.id}-{allocation.id}"
                ),
                related_delivery_id=delivery.id,
            )
            db.flush()
            allocation.consumed_quantity = quantity
            allocation.status = "dispatched"
            allocation.consume_movement_id = movement.id
            allocation.dispatched_by = operator_id
            allocation.dispatched_at = dispatched_at


def _restore_allocation_quantity(
    db: Session,
    *,
    delivery: Delivery,
    delivery_item: DeliveryItem,
    allocation: UnorderedFinishedDeliveryAllocation,
    quantity: int,
    operator_id: int | None,
    reversal_kind: str,
    reason: str,
    idempotency_key: str,
    return_receipt_item_id: int | None = None,
) -> UnorderedFinishedDeliveryReversal:
    # A receipt return can be reconsumed when the receipt is edited/cancelled and
    # then legitimately created again with the same shortage quantity.  Keep the
    # old compensation audit immutable and allocate a new cycle key.  Repeating
    # the same active operation remains idempotent.
    base_idempotency_key = idempotency_key
    cycle = 1
    while True:
        existing = db.scalar(
            select(UnorderedFinishedDeliveryReversal).where(
                UnorderedFinishedDeliveryReversal.idempotency_key
                == idempotency_key
            )
        )
        if existing is None:
            break
        if existing.status == "active":
            return existing
        cycle += 1
        idempotency_key = f"{base_idempotency_key}-cycle-{cycle}"
    remaining = int(allocation.consumed_quantity or 0) - int(
        allocation.restored_quantity or 0
    )
    if quantity <= 0 or quantity > remaining:
        raise WarehouseInventoryError("库存冲回数量超过本次送货原批次余额", 409)
    lot = _validated_customer_lot(
        db,
        delivery=delivery,
        delivery_item=delivery_item,
        allocation=allocation,
    )
    if int(lot.quantity_consumed or 0) < quantity:
        raise WarehouseInventoryError(
            f"批次 {lot.lot_number} 累计出库数量异常，禁止冲回",
            409,
        )
    before = _balances(lot)
    version = int(lot.version)
    now = utc_now_naive()
    changed = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == version,
            InventoryLot.quantity_consumed >= quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available + quantity,
            quantity_consumed=InventoryLot.quantity_consumed - quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise WarehouseInventoryError(
            f"批次 {lot.lot_number} 库存已变化，请刷新后重试",
            409,
        )
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    movement = _movement(
        db,
        lot=lot,
        movement_type="reverse_consume",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason=reason,
        remarks=f"送货明细 {delivery_item.id}",
        idempotency_key=idempotency_key,
        related_delivery_id=delivery.id,
        reversal_of_movement_id=allocation.consume_movement_id,
    )
    db.flush()
    reversal = UnorderedFinishedDeliveryReversal(
        allocation_id=allocation.id,
        return_receipt_item_id=return_receipt_item_id,
        inventory_movement_id=movement.id,
        reversal_kind=reversal_kind,
        reversal_quantity=quantity,
        reason=reason,
        idempotency_key=idempotency_key,
        status="active",
        created_by=operator_id,
    )
    db.add(reversal)
    allocation.restored_quantity = int(allocation.restored_quantity or 0) + quantity
    allocation.status = (
        "restored"
        if allocation.restored_quantity >= allocation.consumed_quantity
        else "partial_restored"
    )
    return reversal


def cancel_unordered_finished_dispatch(
    db: Session,
    *,
    delivery: Delivery,
    delivery_items: list[DeliveryItem],
    operator_id: int | None,
) -> None:
    for delivery_item in delivery_items:
        allocations = db.scalars(
            select(UnorderedFinishedDeliveryAllocation)
            .where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id
                == delivery_item.id
            )
            .order_by(UnorderedFinishedDeliveryAllocation.id)
        ).all()
        for allocation in allocations:
            quantity = int(allocation.consumed_quantity or 0) - int(
                allocation.restored_quantity or 0
            )
            if quantity <= 0:
                continue
            _restore_allocation_quantity(
                db,
                delivery=delivery,
                delivery_item=delivery_item,
                allocation=allocation,
                quantity=quantity,
                operator_id=operator_id,
                reversal_kind="dispatch_cancel",
                reason="取消无订单成品送货，退回原批次",
                idempotency_key=(
                    f"unordered-finished-cancel-{delivery.id}-{allocation.id}"
                ),
            )


def restore_unordered_finished_receipt_shortage(
    db: Session,
    *,
    delivery: Delivery,
    delivery_item: DeliveryItem,
    return_receipt_item_id: int,
    actual_received_quantity: int,
    operator_id: int | None,
    reason: str | None,
) -> None:
    """Automatically return a confirmed short receipt to its original lots."""

    shortage = int(delivery_item.delivered_quantity or 0) - int(
        actual_received_quantity or 0
    )
    if shortage <= 0:
        return
    allocations = db.scalars(
        select(UnorderedFinishedDeliveryAllocation)
        .where(
            UnorderedFinishedDeliveryAllocation.delivery_item_id == delivery_item.id
        )
        .order_by(UnorderedFinishedDeliveryAllocation.id.desc())
    ).all()
    remaining = shortage
    for allocation in allocations:
        available_to_restore = int(allocation.consumed_quantity or 0) - int(
            allocation.restored_quantity or 0
        )
        quantity = min(available_to_restore, remaining)
        if quantity <= 0:
            continue
        _restore_allocation_quantity(
            db,
            delivery=delivery,
            delivery_item=delivery_item,
            allocation=allocation,
            quantity=quantity,
            operator_id=operator_id,
            reversal_kind=(
                "receipt_refusal_return"
                if int(actual_received_quantity or 0) == 0
                else "receipt_short_return"
            ),
            reason=reason or "客户短收，自动退回原成品批次",
            idempotency_key=(
                f"unordered-finished-receipt-{return_receipt_item_id}-"
                f"{allocation.id}-{quantity}"
            ),
            return_receipt_item_id=return_receipt_item_id,
        )
        remaining -= quantity
        if remaining == 0:
            break
    if remaining:
        raise WarehouseInventoryError("客户短收数量超过本次原批次可冲回数量", 409)


def reconsume_unordered_finished_receipt_returns(
    db: Session,
    *,
    return_receipt_item_ids: list[int],
    operator_id: int | None,
) -> None:
    """Undo active receipt returns before editing or cancelling the receipt."""

    if not return_receipt_item_ids:
        return
    reversals = db.scalars(
        select(UnorderedFinishedDeliveryReversal)
        .where(
            UnorderedFinishedDeliveryReversal.return_receipt_item_id.in_(
                return_receipt_item_ids
            ),
            UnorderedFinishedDeliveryReversal.status == "active",
        )
        .order_by(UnorderedFinishedDeliveryReversal.id)
    ).all()
    for reversal in reversals:
        allocation = db.get(
            UnorderedFinishedDeliveryAllocation,
            reversal.allocation_id,
        )
        if allocation is None:
            raise WarehouseInventoryError("回单库存冲回记录不完整", 409)
        delivery_item = db.get(DeliveryItem, allocation.delivery_item_id)
        if delivery_item is None:
            raise WarehouseInventoryError("回单关联送货明细不存在", 409)
        delivery = db.get(Delivery, delivery_item.delivery_id)
        if delivery is None:
            raise WarehouseInventoryError("回单关联送货单不存在", 409)
        lot = _validated_customer_lot(
            db,
            delivery=delivery,
            delivery_item=delivery_item,
            allocation=allocation,
        )
        quantity = int(reversal.reversal_quantity)
        later_consume = db.scalar(
            select(InventoryMovement.id)
            .where(
                InventoryMovement.inventory_lot_id == lot.id,
                InventoryMovement.movement_type == "consume",
                InventoryMovement.id > reversal.inventory_movement_id,
            )
            .order_by(InventoryMovement.id)
            .limit(1)
        )
        if later_consume is not None:
            raise WarehouseInventoryError(
                f"批次 {lot.lot_number} 在本次短收回库后已有新的出库，"
                "不能修改或取消原回单",
                409,
            )
        if int(lot.quantity_available or 0) < quantity:
            raise WarehouseInventoryError(
                f"批次 {lot.lot_number} 的退回库存已被使用，不能修改或取消原回单",
                409,
            )
        before = _balances(lot)
        version = int(lot.version)
        now = utc_now_naive()
        changed = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == version,
                InventoryLot.quantity_available >= quantity,
            )
            .values(
                quantity_available=InventoryLot.quantity_available - quantity,
                quantity_consumed=InventoryLot.quantity_consumed + quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise WarehouseInventoryError(
                f"批次 {lot.lot_number} 库存已变化，请刷新后重试",
                409,
            )
        db.flush()
        db.expire(lot)
        lot = db.get(InventoryLot, lot.id)
        assert lot is not None
        movement = _movement(
            db,
            lot=lot,
            movement_type="consume",
            quantity=quantity,
            before=before,
            operator_id=operator_id,
            reason="撤销或修改回单，补偿原短收回库",
            remarks=f"送货明细 {delivery_item.id}",
            idempotency_key=f"unordered-finished-reconsume-{reversal.id}",
            related_delivery_id=delivery.id,
            reversal_of_movement_id=reversal.inventory_movement_id,
        )
        db.flush()
        allocation.restored_quantity = max(
            int(allocation.restored_quantity or 0) - quantity,
            0,
        )
        allocation.status = (
            "dispatched"
            if allocation.restored_quantity == 0
            else "partial_restored"
        )
        reversal.status = "reconsumed"
        reversal.reconsumed_movement_id = movement.id
        reversal.reconsumed_by = operator_id
        reversal.reconsumed_at = now

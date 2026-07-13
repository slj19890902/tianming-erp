from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import re
import unicodedata
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.delivery import DeliveryItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.requisition import RequisitionItem
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    SemiFinishedInventoryDetail,
    WarehouseLocation,
)
from app.services.inventory_cost_snapshot import (
    apply_cost_snapshot,
    estimate_finished_product_cost,
    estimate_semi_finished_cost,
)


class WarehouseInventoryError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AgeWarning:
    days: int
    level: str | None
    text: str | None


@dataclass(frozen=True)
class FinishedReservationMutation:
    reservation: InventoryReservation
    movement: InventoryMovement
    allocation: DeliveryInventoryAllocation | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_material_code(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        raise WarehouseInventoryError("材质代码不能为空")
    return normalized


def inventory_age_warning(lot: InventoryLot, *, today: date | None = None) -> AgeWarning:
    current = today or date.today()
    base_date = (lot.last_movement_at.date() if lot.last_movement_at else lot.stock_date)
    days = max((current - base_date).days, 0)
    if days >= 730:
        return AgeWarning(days, "cleanup", "超过2年未变动，请盘点并处理")
    if days >= 548:
        return AgeWarning(days, "handling", "超过18个月未变动，请安排处理")
    if days >= 365:
        return AgeWarning(days, "attention", "超过1年未变动，请重点关注")
    return AgeWarning(days, None, None)


def _number(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d}-{uuid4().hex[:10].upper()}"


def _location(db: Session, location_id: int, inventory_type: str) -> WarehouseLocation:
    location = db.get(WarehouseLocation, location_id)
    allowed = {
        "finished": {"finished", "shared"},
        "semi_finished": {"semi_finished", "shared"},
    }[inventory_type]
    if location is None:
        raise WarehouseInventoryError("库位不存在", 404)
    if not location.is_active:
        raise WarehouseInventoryError("该库位已停用，不能入库")
    if location.warehouse_type not in allowed:
        raise WarehouseInventoryError("所选库位类型与库存类型不匹配")
    return location


def _idempotent_lot(db: Session, key: str | None) -> InventoryLot | None:
    if not key:
        return None
    movement = db.scalar(
        select(InventoryMovement).where(InventoryMovement.idempotency_key == key)
    )
    return db.get(InventoryLot, movement.inventory_lot_id) if movement else None


def _balances(lot: InventoryLot) -> dict[str, int]:
    return {
        "available": lot.quantity_available,
        "reserved": lot.quantity_reserved,
        "consumed": lot.quantity_consumed,
        "damaged": lot.quantity_damaged,
        "scrapped": lot.quantity_scrapped,
    }


def _movement(
    db: Session,
    *,
    lot: InventoryLot,
    movement_type: str,
    quantity: int,
    before: dict[str, int],
    operator_id: int | None,
    reason: str | None = None,
    remarks: str | None = None,
    idempotency_key: str | None = None,
    reservation_id: int | None = None,
    related_order_id: int | None = None,
    related_order_item_id: int | None = None,
    related_delivery_id: int | None = None,
    reversal_of_movement_id: int | None = None,
) -> InventoryMovement:
    after = _balances(lot)
    row = InventoryMovement(
        movement_number=_number("IM"),
        inventory_lot_id=lot.id,
        movement_type=movement_type,
        quantity=abs(quantity),
        unit=lot.unit,
        before_available=before["available"],
        after_available=after["available"],
        before_reserved=before["reserved"],
        after_reserved=after["reserved"],
        before_consumed=before["consumed"],
        after_consumed=after["consumed"],
        before_damaged=before["damaged"],
        after_damaged=after["damaged"],
        before_scrapped=before["scrapped"],
        after_scrapped=after["scrapped"],
        reason=reason,
        remarks=remarks,
        operator_id=operator_id,
        idempotency_key=idempotency_key,
        reservation_id=reservation_id,
        related_order_id=related_order_id,
        related_order_item_id=related_order_item_id,
        related_delivery_id=related_delivery_id,
        reversal_of_movement_id=reversal_of_movement_id,
    )
    db.add(row)
    return row


def manual_finished_in(
    db: Session,
    *,
    customer_id: int,
    product_id: int,
    location_id: int,
    quantity: int,
    stock_date: date,
    source_type: str,
    remarks: str | None,
    operator_id: int | None,
    idempotency_key: str | None,
    source_ref_type: str | None = None,
    source_ref_id: int | None = None,
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("入库数量必须大于0")
    _location(db, location_id, "finished")
    customer = db.get(Customer, customer_id)
    product = db.get(Product, product_id)
    if customer is None:
        raise WarehouseInventoryError("客户不存在", 404)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if product.customer_id != customer_id:
        raise WarehouseInventoryError("所选产品不属于该客户")
    now = utc_now()
    material_code = (
        product.material.code if product.material is not None else product.default_material_code
    ) or product.legacy_material_text
    lot = InventoryLot(
        lot_number=_number("FG"),
        inventory_type="finished",
        warehouse_location_id=location_id,
        quantity_available=quantity,
        unit="boxes",
        status="active",
        source_type=source_type,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        stock_date=stock_date,
        last_movement_at=now,
        remarks=remarks,
        created_by=operator_id,
    )
    apply_cost_snapshot(
        lot,
        estimate_finished_product_cost(
            db,
            product=product,
            material_code=material_code,
            flute_type=product.flute_type,
        ),
        captured_at=now,
    )
    db.add(lot)
    db.flush()
    lot.finished_detail = FinishedGoodsInventoryDetail(
        owner_customer_id=customer.id,
        owner_customer_name_snapshot=customer.name,
        is_general=False,
        product_id=product.id,
        inventory_code_snapshot=product.product_code,
        product_name_snapshot=product.product_name,
        box_type_snapshot=product.box_style,
        length_mm=round(product.length_mm) if product.length_mm is not None else None,
        width_mm=round(product.width_mm) if product.width_mm is not None else None,
        height_mm=round(product.height_mm) if product.height_mm is not None else None,
        material_code_snapshot=material_code,
        flute_type_snapshot=product.flute_type,
    )
    _movement(
        db,
        lot=lot,
        movement_type="manual_in",
        quantity=quantity,
        before={key: 0 for key in _balances(lot)},
        operator_id=operator_id,
        reason="手工成品入库",
        remarks=remarks,
        idempotency_key=idempotency_key,
    )
    db.flush()
    return lot


def active_finished_reserved_qty(db: Session, order_item_id: int) -> int:
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status != "cancelled",
        )
    ).all()
    return sum(
        max(
            int(row.credited_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
        for row in rows
    )


def active_finished_reservations_by_item_ids(
    db: Session, order_item_ids: list[int]
) -> dict[int, int]:
    if not order_item_ids:
        return {}
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.order_item_id.in_(order_item_ids),
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status != "cancelled",
        )
    ).all()
    result: dict[int, int] = {}
    for row in rows:
        if row.order_item_id is None:
            continue
        result[row.order_item_id] = result.get(row.order_item_id, 0) + max(
            int(row.credited_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
    return result


def finished_inventory_candidates(db: Session, order_item_id: int) -> list[InventoryLot]:
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    if item.requisition_status != "未报料":
        raise WarehouseInventoryError("订单已进入报料，请先取消报料后再抵扣成品库存", 409)
    return db.scalars(
        select(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            FinishedGoodsInventoryDetail.product_id == item.product_id,
            or_(
                FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
                FinishedGoodsInventoryDetail.is_general.is_(True),
            ),
        )
        .order_by(
            FinishedGoodsInventoryDetail.is_general,
            InventoryLot.stock_date,
            InventoryLot.id,
        )
    ).all()


def finished_inventory_candidates_for_product(
    db: Session,
    *,
    customer_id: int,
    product_id: int,
) -> list[InventoryLot]:
    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if product.customer_id != customer_id:
        raise WarehouseInventoryError("产品不属于所选客户", 409)
    return db.scalars(
        select(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            FinishedGoodsInventoryDetail.product_id == product_id,
            FinishedGoodsInventoryDetail.owner_customer_id == customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.inventory_code_snapshot
            == product.product_code,
        )
        .order_by(
            InventoryLot.stock_date,
            InventoryLot.id,
        )
    ).all()


def has_unconsumed_inventory_reservations(
    db: Session,
    order_item_id: int,
) -> bool:
    return (
        db.scalar(
            select(InventoryReservation.id)
            .where(
                InventoryReservation.order_item_id == order_item_id,
                InventoryReservation.status != "cancelled",
                InventoryReservation.reserved_stock_quantity
                > InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity,
            )
            .limit(1)
        )
        is not None
    )


def reserve_finished_inventory(
    db: Session,
    *,
    order_item_id: int,
    inventory_lot_id: int,
    quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    warning_acknowledged_codes: list[str],
) -> InventoryReservation:
    existing = db.scalar(
        select(InventoryReservation).where(
            InventoryReservation.idempotency_key == idempotency_key
        )
    )
    if existing:
        if (
            existing.order_item_id != order_item_id
            or existing.inventory_lot_id != inventory_lot_id
        ):
            raise WarehouseInventoryError("该请求标识已用于其他库存预占", 409)
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("成品库存抵扣数量必须大于0")
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    if item.requisition_status != "未报料":
        raise WarehouseInventoryError("订单已进入报料，请先取消报料后再抵扣成品库存", 409)
    if item.material_status == "received" or item.delivered_quantity > 0:
        raise WarehouseInventoryError("订单明细已进入后续流程，不能新增成品库存抵扣", 409)
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError("成品库存批次不存在", 404)
    detail = lot.finished_detail
    if lot.inventory_type != "finished" or lot.status != "active":
        raise WarehouseInventoryError("该库存批次当前不可预占", 409)
    if detail.product_id != item.product_id:
        raise WarehouseInventoryError("库存产品与订单产品不一致")
    if not detail.is_general and detail.owner_customer_id != order.customer_id:
        raise WarehouseInventoryError("客户专用库存不能用于其他客户订单")
    warning_codes: list[str] = []
    if detail.is_general:
        warning_codes.append("GENERAL_FINISHED_STOCK")
        if "GENERAL_FINISHED_STOCK" not in warning_acknowledged_codes:
            raise WarehouseInventoryError("通用库存必须人工确认后才能用于该客户订单")
    already_reserved = active_finished_reserved_qty(db, item.id)
    remaining_requirement = max(item.quantity - already_reserved, 0)
    if quantity > remaining_requirement:
        raise WarehouseInventoryError(
            f"抵扣数量不能超过订单剩余可抵扣数量 {remaining_requirement}"
        )
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新候选后重试", 409)
    if quantity > lot.quantity_available:
        raise WarehouseInventoryError("抵扣数量不能超过库存可用数量", 409)
    before = _balances(lot)
    now = utc_now()
    result = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == expected_version,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available >= quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available - quantity,
            quantity_reserved=InventoryLot.quantity_reserved + quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新候选后重试", 409)
    reservation = InventoryReservation(
        reservation_number=_number("RS"),
        inventory_lot_id=lot.id,
        reservation_type="finished_order",
        order_id=order.id,
        order_item_id=item.id,
        reserved_stock_quantity=quantity,
        credited_requirement_quantity=quantity,
        yield_factor=1,
        status="active",
        warning_codes=json.dumps(warning_codes, ensure_ascii=False),
        warning_acknowledged_by=operator_id if warning_codes else None,
        reserved_by=operator_id,
        reserved_at=now,
        idempotency_key=idempotency_key,
    )
    db.add(reservation)
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    _movement(
        db,
        lot=lot,
        movement_type="reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason="成品库存抵扣订单",
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=order.id,
        related_order_item_id=item.id,
    )
    db.flush()
    return reservation


def _finished_reservation_status(reservation: InventoryReservation) -> str:
    consumed = int(reservation.consumed_stock_quantity or 0)
    released = int(reservation.released_stock_quantity or 0)
    remaining = int(reservation.reserved_stock_quantity) - consumed - released
    if remaining > 0:
        return "active" if consumed == 0 and released == 0 else "partial"
    if consumed == reservation.reserved_stock_quantity:
        return "consumed"
    if released == reservation.reserved_stock_quantity:
        return "released"
    return "partial"


def _finished_idempotent_mutation(
    db: Session,
    *,
    idempotency_key: str,
    movement_type: str,
    reservation_id: int,
) -> FinishedReservationMutation | None:
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if movement is None:
        return None
    if movement.movement_type != movement_type or movement.reservation_id != reservation_id:
        raise WarehouseInventoryError("该请求标识已用于其他库存操作", 409)
    reservation = db.get(InventoryReservation, reservation_id)
    if reservation is None:
        raise WarehouseInventoryError("库存预占记录不存在", 404)
    consume_movement_id = (
        movement.reversal_of_movement_id
        if movement_type == "reverse_consume"
        else movement.id
    )
    allocation = db.scalar(
        select(DeliveryInventoryAllocation).where(
            DeliveryInventoryAllocation.consume_movement_id == consume_movement_id
        )
    )
    return FinishedReservationMutation(reservation, movement, allocation)


def consume_finished_reservation(
    db: Session,
    *,
    reservation_id: int,
    stock_quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    delivery_item_id: int,
) -> FinishedReservationMutation:
    repeated = _finished_idempotent_mutation(
        db,
        idempotency_key=idempotency_key,
        movement_type="consume",
        reservation_id=reservation_id,
    )
    if repeated is not None:
        return repeated
    if stock_quantity <= 0:
        raise WarehouseInventoryError("成品消耗数量必须大于0")
    with db.begin_nested():
        reservation = db.get(InventoryReservation, reservation_id)
        if reservation is None:
            raise WarehouseInventoryError("库存预占记录不存在", 404)
        if reservation.reservation_type != "finished_order":
            raise WarehouseInventoryError("该记录不是成品订单预占")
        remaining = (
            int(reservation.reserved_stock_quantity)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0)
        )
        if stock_quantity > remaining:
            raise WarehouseInventoryError("消耗数量不能超过未消耗成品预占余额", 409)
        delivery_item = db.get(DeliveryItem, delivery_item_id)
        if delivery_item is None:
            raise WarehouseInventoryError("送货明细不存在", 404)
        if delivery_item.order_item_id != reservation.order_item_id:
            raise WarehouseInventoryError("送货明细与成品预占订单不一致", 409)
        order_item = db.get(OrderItem, reservation.order_item_id)
        order = db.get(Order, reservation.order_id)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if order_item is None or order is None or lot is None or lot.finished_detail is None:
            raise WarehouseInventoryError("成品预占关联数据不完整", 409)
        detail = lot.finished_detail
        if detail.product_id != order_item.product_id:
            raise WarehouseInventoryError("成品库存与订单产品不一致", 409)
        if not detail.is_general and detail.owner_customer_id != order.customer_id:
            raise WarehouseInventoryError("其他客户专用成品库存不能用于当前订单", 409)
        if lot.version != expected_version:
            raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
        if lot.quantity_reserved < stock_quantity:
            raise WarehouseInventoryError("成品库存预占余额异常，请联系管理员", 409)
        before = _balances(lot)
        now = utc_now()
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_reserved >= stock_quantity,
            )
            .values(
                quantity_reserved=InventoryLot.quantity_reserved - stock_quantity,
                quantity_consumed=InventoryLot.quantity_consumed + stock_quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if result.rowcount != 1:
            raise WarehouseInventoryError("成品库存数量或版本已变化，请重试", 409)
        reservation.consumed_stock_quantity += stock_quantity
        reservation.consumed_requirement_quantity += stock_quantity
        reservation.consumed_by = operator_id
        reservation.consumed_at = now
        reservation.status = _finished_reservation_status(reservation)
        db.flush()
        db.expire(lot)
        refreshed_lot = db.get(InventoryLot, lot.id)
        assert refreshed_lot is not None
        movement = _movement(
            db,
            lot=refreshed_lot,
            movement_type="consume",
            quantity=stock_quantity,
            before=before,
            operator_id=operator_id,
            reason="送货出库消耗成品预占",
            idempotency_key=idempotency_key,
            reservation_id=reservation.id,
            related_order_id=reservation.order_id,
            related_order_item_id=reservation.order_item_id,
            related_delivery_id=delivery_item.delivery_id,
        )
        db.flush()
        allocation = DeliveryInventoryAllocation(
            delivery_item_id=delivery_item.id,
            reservation_id=reservation.id,
            consume_movement_id=movement.id,
            consumed_stock_quantity=stock_quantity,
            credited_requirement_quantity=stock_quantity,
            reversed_stock_quantity=0,
            reversed_requirement_quantity=0,
            status="active",
            created_by=operator_id,
        )
        db.add(allocation)
        db.flush()
    return FinishedReservationMutation(reservation, movement, allocation)


def reverse_finished_consumption(
    db: Session,
    *,
    reservation_id: int,
    stock_quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    allocation_id: int,
) -> FinishedReservationMutation:
    repeated = _finished_idempotent_mutation(
        db,
        idempotency_key=idempotency_key,
        movement_type="reverse_consume",
        reservation_id=reservation_id,
    )
    if repeated is not None:
        return repeated
    if stock_quantity <= 0:
        raise WarehouseInventoryError("成品逆转数量必须大于0")
    with db.begin_nested():
        reservation = db.get(InventoryReservation, reservation_id)
        allocation = db.get(DeliveryInventoryAllocation, allocation_id)
        if reservation is None or reservation.reservation_type != "finished_order":
            raise WarehouseInventoryError("成品库存预占记录不存在", 404)
        if allocation is None or allocation.reservation_id != reservation.id:
            raise WarehouseInventoryError("送货成品库存分配记录不存在", 404)
        allocation_remaining = (
            int(allocation.consumed_stock_quantity)
            - int(allocation.reversed_stock_quantity or 0)
        )
        if stock_quantity > allocation_remaining:
            raise WarehouseInventoryError("逆转数量不能超过该送货成品分配余额", 409)
        if stock_quantity > int(reservation.consumed_stock_quantity or 0):
            raise WarehouseInventoryError("逆转数量不能超过累计成品消耗", 409)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None or lot.version != expected_version:
            raise WarehouseInventoryError("成品库存版本已变化，请重试", 409)
        if lot.quantity_consumed < stock_quantity:
            raise WarehouseInventoryError("成品库存累计消耗余额异常", 409)
        before = _balances(lot)
        now = utc_now()
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_consumed >= stock_quantity,
            )
            .values(
                quantity_reserved=InventoryLot.quantity_reserved + stock_quantity,
                quantity_consumed=InventoryLot.quantity_consumed - stock_quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if result.rowcount != 1:
            raise WarehouseInventoryError("成品库存数量或版本已变化，请重试", 409)
        reservation.consumed_stock_quantity -= stock_quantity
        reservation.consumed_requirement_quantity -= stock_quantity
        if reservation.consumed_stock_quantity == 0:
            reservation.consumed_by = None
            reservation.consumed_at = None
        reservation.status = _finished_reservation_status(reservation)
        allocation.reversed_stock_quantity += stock_quantity
        allocation.reversed_requirement_quantity += stock_quantity
        allocation.reversed_by = operator_id
        allocation.reversed_at = now
        allocation.status = (
            "reversed"
            if allocation.reversed_stock_quantity == allocation.consumed_stock_quantity
            else "partial"
        )
        db.flush()
        db.expire(lot)
        refreshed_lot = db.get(InventoryLot, lot.id)
        assert refreshed_lot is not None
        movement = _movement(
            db,
            lot=refreshed_lot,
            movement_type="reverse_consume",
            quantity=stock_quantity,
            before=before,
            operator_id=operator_id,
            reason="撤销送货出库成品消耗",
            idempotency_key=idempotency_key,
            reservation_id=reservation.id,
            related_order_id=reservation.order_id,
            related_order_item_id=reservation.order_item_id,
            related_delivery_id=db.get(DeliveryItem, allocation.delivery_item_id).delivery_id,
            reversal_of_movement_id=allocation.consume_movement_id,
        )
        db.flush()
    return FinishedReservationMutation(reservation, movement, allocation)


def release_finished_reservation(
    db: Session,
    *,
    reservation_id: int,
    operator_id: int | None,
    release_reason: str,
    idempotency_key: str,
    allow_downstream: bool = False,
) -> InventoryReservation:
    repeated = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if repeated:
        if repeated.movement_type != "release_reserve":
            raise WarehouseInventoryError(
                "该请求标识已用于其他库存操作，请重新提交", 409
            )
        reservation = db.get(InventoryReservation, repeated.reservation_id)
        if reservation is None or reservation.id != reservation_id:
            raise WarehouseInventoryError("该请求标识已用于其他释放操作", 409)
        return reservation
    reservation = db.get(InventoryReservation, reservation_id)
    if reservation is None:
        raise WarehouseInventoryError("库存预占记录不存在", 404)
    if reservation.reservation_type != "finished_order":
        raise WarehouseInventoryError("该记录不是成品订单预占")
    remaining = (
        int(reservation.reserved_stock_quantity)
        - int(reservation.consumed_stock_quantity or 0)
        - int(reservation.released_stock_quantity or 0)
    )
    if remaining <= 0:
        raise WarehouseInventoryError("该预占已释放或已消耗，不能重复释放", 409)
    reason = release_reason.strip()
    if not reason:
        raise WarehouseInventoryError("取消抵扣必须填写原因")
    item = db.get(OrderItem, reservation.order_item_id) if reservation.order_item_id else None
    if item is not None and not allow_downstream:
        if item.requisition_status != "未报料":
            raise WarehouseInventoryError("请先取消报料，再取消成品库存抵扣", 409)
        if db.scalar(
            select(DeliveryItem.id)
            .where(DeliveryItem.order_item_id == item.id)
            .limit(1)
        ):
            raise WarehouseInventoryError("订单已进入送货流程，不能取消成品库存抵扣", 409)
    lot = db.get(InventoryLot, reservation.inventory_lot_id)
    if lot is None:
        raise WarehouseInventoryError("关联库存批次不存在", 409)
    quantity = remaining
    if lot.quantity_reserved < quantity:
        raise WarehouseInventoryError("库存预占余额异常，请联系管理员处理", 409)
    before = _balances(lot)
    now = utc_now()
    result = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == lot.version,
            InventoryLot.quantity_reserved >= quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available + quantity,
            quantity_reserved=InventoryLot.quantity_reserved - quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存数量已变化，请刷新后重试", 409)
    reservation.released_stock_quantity += quantity
    reservation.released_requirement_quantity += quantity
    reservation.status = _finished_reservation_status(reservation)
    reservation.released_by = operator_id
    reservation.released_at = now
    reservation.release_reason = reason
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    _movement(
        db,
        lot=lot,
        movement_type="release_reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason=reason,
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=reservation.order_id,
        related_order_item_id=reservation.order_item_id,
    )
    db.flush()
    return reservation


def release_active_finished_reservations_for_items(
    db: Session,
    *,
    order_item_ids: list[int],
    operator_id: int | None,
    reason: str,
    idempotency_prefix: str,
    allow_downstream: bool = False,
) -> list[InventoryReservation]:
    if not order_item_ids:
        return []
    rows = db.scalars(
        select(InventoryReservation)
        .where(
            InventoryReservation.order_item_id.in_(order_item_ids),
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .order_by(InventoryReservation.id)
    ).all()
    return [
        release_finished_reservation(
            db,
            reservation_id=row.id,
            operator_id=operator_id,
            release_reason=reason,
            idempotency_key=f"{idempotency_prefix}-{row.id}",
            allow_downstream=allow_downstream,
        )
        for row in rows
    ]


def manual_semi_finished_in(
    db: Session,
    *,
    location_id: int,
    quantity: int,
    stock_date: date,
    source_type: str,
    material_code: str,
    layer_count: int,
    flute_type: str,
    board_length_mm: int,
    board_width_mm: int,
    sheet_type: str,
    component_type: str = "whole",
    pieces_per_box: int = 1,
    stock_yield_per_sheet: int = 1,
    supplier_name: str | None,
    customer_id: int | None,
    crease_type: str | None,
    crease_left_mm: int | None,
    crease_middle_mm: int | None,
    crease_right_mm: int | None,
    cutting_note: str | None,
    remarks: str | None,
    operator_id: int | None,
    idempotency_key: str | None,
    source_ref_type: str | None = None,
    source_ref_id: int | None = None,
    material_id: int | None = None,
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        return existing
    if quantity <= 0 or board_length_mm <= 0 or board_width_mm <= 0:
        raise WarehouseInventoryError("数量和纸板长宽必须大于0")
    component = component_type.strip().lower()
    if component not in {"whole", "cover", "base"}:
        raise WarehouseInventoryError("半成品组件仅允许整片、天地盖盖片或底片")
    if pieces_per_box <= 0 or stock_yield_per_sheet <= 0:
        raise WarehouseInventoryError("每箱片数和每库存张产出片数必须大于0")
    valid_flutes = {3: {"A", "B", "E"}, 5: {"AB", "BE"}}
    flute = flute_type.strip().upper()
    if layer_count not in valid_flutes or flute not in valid_flutes[layer_count]:
        raise WarehouseInventoryError("三层仅支持A/B/E楞，五层仅支持AB/BE楞")
    if sheet_type not in {"raw_board", "net_sheet", "creased_sheet"}:
        raise WarehouseInventoryError("片料类型无效")
    _location(db, location_id, "semi_finished")
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer_id and customer is None:
        raise WarehouseInventoryError("客户不存在", 404)
    material = db.get(Material, material_id) if material_id else None
    if material_id and (material is None or not material.is_active):
        raise WarehouseInventoryError("材质主数据不存在或已停用", 404)
    if material is not None:
        material_code = material.code
        if material.layer_count is not None:
            layer_count = material.layer_count
    now = utc_now()
    lot = InventoryLot(
        lot_number=_number("SI"),
        inventory_type="semi_finished",
        warehouse_location_id=location_id,
        quantity_available=quantity,
        unit="sheets",
        status="active",
        source_type=source_type,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        stock_date=stock_date,
        last_movement_at=now,
        remarks=remarks,
        created_by=operator_id,
    )
    apply_cost_snapshot(
        lot,
        estimate_semi_finished_cost(
            db,
            material_id=material.id if material else None,
            material_code=material_code,
            supplier_name=supplier_name,
            layer_count=layer_count,
            flute_type=flute,
            board_length_mm=board_length_mm,
            board_width_mm=board_width_mm,
        ),
        captured_at=now,
    )
    db.add(lot)
    db.flush()
    lot.semi_finished_detail = SemiFinishedInventoryDetail(
        supplier_name=(supplier_name or "").strip() or None,
        owner_customer_id=customer.id if customer else None,
        owner_customer_name_snapshot=customer.name if customer else None,
        material_id=material.id if material else None,
        material_code_snapshot=material_code.strip(),
        normalized_material_code=normalize_material_code(material_code),
        layer_count=layer_count,
        flute_type=flute,
        board_length_mm=board_length_mm,
        board_width_mm=board_width_mm,
        component_type=component,
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=stock_yield_per_sheet,
        sheet_type=sheet_type,
        crease_type=crease_type,
        crease_left_mm=crease_left_mm,
        crease_middle_mm=crease_middle_mm,
        crease_right_mm=crease_right_mm,
        cutting_note=cutting_note,
    )
    _movement(
        db,
        lot=lot,
        movement_type="manual_in",
        quantity=quantity,
        before={key: 0 for key in _balances(lot)},
        operator_id=operator_id,
        reason="手工半成品入库",
        remarks=remarks,
        idempotency_key=idempotency_key,
    )
    db.flush()
    return lot


def mutate_lot(
    db: Session,
    *,
    lot_id: int,
    operation: str,
    expected_version: int,
    operator_id: int | None,
    quantity: int = 0,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        return existing
    lot = db.get(InventoryLot, lot_id)
    if lot is None:
        raise WarehouseInventoryError("库存批次不存在", 404)
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    if lot.status == "closed":
        raise WarehouseInventoryError("已关闭库存不能操作", 409)
    if operation in {"adjust", "damage", "scrap", "transfer_to_general"} and lot.status != "active":
        raise WarehouseInventoryError("冻结库存不能执行数量或归属调整", 409)
    before = _balances(lot)
    values: dict[str, object] = {"version": lot.version + 1, "last_movement_at": utc_now()}
    movement_quantity = quantity
    if operation == "freeze":
        if lot.status != "active":
            raise WarehouseInventoryError("只有正常库存可以冻结", 409)
        values["status"] = "frozen"
        movement_quantity = 0
    elif operation == "unfreeze":
        if lot.status != "frozen":
            raise WarehouseInventoryError("只有冻结库存可以解冻", 409)
        values["status"] = "active"
        movement_quantity = 0
    elif operation == "adjust":
        if quantity == 0:
            raise WarehouseInventoryError("调整数量不能为0")
        if lot.quantity_available + quantity < 0:
            raise WarehouseInventoryError("调整后可用库存不能小于0")
        values["quantity_available"] = lot.quantity_available + quantity
    elif operation in {"damage", "scrap"}:
        if quantity <= 0:
            raise WarehouseInventoryError("数量必须大于0")
        if quantity > lot.quantity_available:
            raise WarehouseInventoryError("数量不能大于当前可用库存")
        values["quantity_available"] = lot.quantity_available - quantity
        values[f"quantity_{'damaged' if operation == 'damage' else 'scrapped'}"] = (
            lot.quantity_damaged + quantity
            if operation == "damage"
            else lot.quantity_scrapped + quantity
        )
    elif operation == "transfer_to_general":
        if lot.inventory_type != "finished" or lot.finished_detail is None:
            raise WarehouseInventoryError("只有成品库存可以转为通用库存")
        if lot.finished_detail.is_general:
            raise WarehouseInventoryError("该批次已经是通用库存", 409)
        movement_quantity = 0
    else:
        raise WarehouseInventoryError("不支持的库存操作")
    result = db.execute(
        update(InventoryLot)
        .where(InventoryLot.id == lot.id, InventoryLot.version == expected_version)
        .values(**values)
    )
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    if operation == "transfer_to_general":
        lot.finished_detail.is_general = True
        lot.finished_detail.owner_customer_id = None
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    _movement(
        db,
        lot=lot,
        movement_type=operation,
        quantity=movement_quantity,
        before=before,
        operator_id=operator_id,
        reason=(reason or "").strip() or None,
        idempotency_key=idempotency_key,
    )
    db.flush()
    return lot

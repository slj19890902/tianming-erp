from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
import unicodedata
from uuid import uuid4

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_now_naive, beijing_today, utc_now_naive
from app.models.customer import Customer
from app.models.delivery import DeliveryItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.production import ProductionCompletion
from app.models.requisition import RequisitionItem
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryLocationMovement,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    WarehouseLocation,
)
from app.services.flute_mapping import seven_layer_code_error
from app.services.inventory_cost_snapshot import (
    apply_cost_snapshot,
    estimate_finished_product_cost,
    estimate_semi_finished_cost,
)
from app.services.location_candidates import operational_location_issue


class WarehouseInventoryError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _delivery_pallet_release_key(delivery_id: int, pallet_id: int) -> str:
    return f"delivery-{delivery_id}-auto-release-pallet-{pallet_id}"


def _pallet_has_physical_goods(db: Session, pallet_id: int) -> bool:
    snapshot_exists = db.scalar(
        select(InventoryPalletItem.id)
        .where(
            InventoryPalletItem.pallet_id == pallet_id,
            InventoryPalletItem.inventory_lot_id.is_(None),
            InventoryPalletItem.quantity > 0,
        )
        .limit(1)
    )
    if snapshot_exists is not None:
        return True
    live_lot_exists = db.scalar(
        select(InventoryLot.id)
        .join(
            InventoryPalletItem,
            InventoryPalletItem.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryPalletItem.pallet_id == pallet_id,
            (
                InventoryLot.quantity_available
                + InventoryLot.quantity_reserved
                + InventoryLot.quantity_damaged
            )
            > 0,
        )
        .limit(1)
    )
    return live_lot_exists is not None


def release_empty_pallets_after_delivery(
    db: Session,
    *,
    delivery_id: int,
    operator_id: int | None,
) -> list[int]:
    """Release only pallets emptied by this formal delivery transaction."""
    db.flush()
    pallet_ids = list(
        db.scalars(
            select(InventoryPalletItem.pallet_id)
            .join(
                InventoryMovement,
                InventoryMovement.inventory_lot_id
                == InventoryPalletItem.inventory_lot_id,
            )
            .where(
                InventoryMovement.related_delivery_id == delivery_id,
                InventoryMovement.movement_type == "consume",
            )
            .distinct()
            .order_by(InventoryPalletItem.pallet_id)
        )
    )
    if not pallet_ids:
        return []

    from app.services.floor3_locations import Floor3LocationError, clear_pallet

    released: list[int] = []
    for pallet_id in pallet_ids:
        pallet = db.get(InventoryPallet, pallet_id)
        if pallet is None or not pallet.is_current or pallet.location_id is None:
            continue
        if _pallet_has_physical_goods(db, pallet_id):
            continue
        try:
            clear_pallet(
                db,
                pallet_id=pallet_id,
                expected_version=int(pallet.version),
                remarks=f"送货单 {delivery_id} 正式发货后货物清零，自动释放空栈板",
                operator_id=operator_id,
                idempotency_key=_delivery_pallet_release_key(delivery_id, pallet_id),
            )
        except Floor3LocationError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error
        released.append(int(pallet_id))
    db.flush()
    return released


def restore_auto_released_pallets_after_delivery_cancel(
    db: Session,
    *,
    delivery_id: int,
    operator_id: int | None,
) -> list[int]:
    """Restore pallet projection when cancelling a dispatch restores its stock."""
    db.flush()
    prefix = f"delivery-{delivery_id}-auto-release-pallet-"
    clear_movements = list(
        db.scalars(
            select(InventoryLocationMovement)
            .where(
                InventoryLocationMovement.movement_type == "clear",
                InventoryLocationMovement.idempotency_key.like(f"{prefix}%"),
            )
            .order_by(InventoryLocationMovement.pallet_id)
        )
    )
    restored: list[int] = []
    for clear_movement in clear_movements:
        pallet = db.get(InventoryPallet, clear_movement.pallet_id)
        if pallet is None or not _pallet_has_physical_goods(db, pallet.id):
            continue
        restore_key = f"{clear_movement.idempotency_key}:restore"
        existing = db.scalar(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.idempotency_key == restore_key
            )
        )
        if existing is not None:
            if existing.pallet_id != pallet.id or existing.movement_type != "move":
                raise WarehouseInventoryError("栈板恢复幂等键已用于其他业务", 409)
            continue
        target_location_id = clear_movement.from_location_id
        if target_location_id is None:
            raise WarehouseInventoryError("空栈板缺少原库位，无法安全取消发货", 409)
        target_location = db.get(WarehouseLocation, target_location_id)
        if target_location is None or not target_location.is_active:
            raise WarehouseInventoryError("空栈板原库位已停用，无法安全取消发货", 409)
        if pallet.is_current or pallet.location_id is not None:
            raise WarehouseInventoryError("空栈板状态已变化，无法安全取消发货", 409)
        occupied = db.scalar(
            select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id == target_location_id,
                InventoryPallet.is_current.is_(True),
                InventoryPallet.id != pallet.id,
            )
            .limit(1)
        )
        if occupied is not None:
            raise WarehouseInventoryError(
                "原库位已被其他栈板占用，无法取消发货；请先腾空原库位后重试",
                409,
            )
        misplaced_lot = db.scalar(
            select(InventoryLot.id)
            .join(
                InventoryPalletItem,
                InventoryPalletItem.inventory_lot_id == InventoryLot.id,
            )
            .where(
                InventoryPalletItem.pallet_id == pallet.id,
                or_(
                    InventoryLot.warehouse_location_id.is_(None),
                    InventoryLot.warehouse_location_id != target_location_id,
                ),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
            .limit(1)
        )
        if misplaced_lot is not None:
            raise WarehouseInventoryError("恢复库存位置与原栈板不一致，无法安全取消发货", 409)

        version_before = int(pallet.version)
        changed = db.execute(
            update(InventoryPallet)
            .where(
                InventoryPallet.id == pallet.id,
                InventoryPallet.version == version_before,
                InventoryPallet.is_current.is_(False),
                InventoryPallet.location_id.is_(None),
            )
            .values(
                location_id=target_location_id,
                status="active",
                is_current=True,
                needs_relocation=bool(target_location.is_temporary),
                version=InventoryPallet.version + 1,
                closed_at=None,
                updated_by=operator_id,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise WarehouseInventoryError("空栈板状态已变化，请刷新后重试", 409)
        now = beijing_now_naive()
        db.add(
            InventoryLocationMovement(
                pallet_id=pallet.id,
                from_location_id=None,
                to_location_id=target_location_id,
                movement_type="move",
                operator_id=operator_id,
                moved_at=now,
                idempotency_key=restore_key,
                confirmed_at=now,
                pallet_version_before=version_before,
                pallet_version_after=version_before + 1,
                remarks=f"取消送货单 {delivery_id}，恢复原库存与栈板位置",
            )
        )
        restored.append(int(pallet.id))
    db.flush()
    return restored


STOCK_DATE_ACCURACIES = frozenset({"exact", "estimated", "unknown"})


SEMI_FINISHED_FLUTES_BY_LAYER: dict[int, frozenset[str]] = {
    3: frozenset({"A", "B", "E"}),
    5: frozenset({"AB", "BE"}),
    7: frozenset({"AAA", "ABC"}),
}


@dataclass(frozen=True)
class AgeWarning:
    days: int | None
    level: str | None
    text: str | None


@dataclass(frozen=True)
class FinishedReservationMutation:
    reservation: InventoryReservation
    movement: InventoryMovement
    allocation: DeliveryInventoryAllocation | None = None


@dataclass(frozen=True)
class FinishedLotLocationTransferResult:
    transfer: InventoryLotTransfer
    source_lot: InventoryLot
    target_lot: InventoryLot
    replayed: bool


# Compatibility export for service modules outside N033's write scope.
utc_now = utc_now_naive


def normalize_material_code(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        raise WarehouseInventoryError("材质代码不能为空")
    return normalized


def normalize_stock_date_metadata(
    *,
    stock_date: date,
    stock_date_accuracy: str = "exact",
    stock_date_original_text: str | None = None,
) -> tuple[str, str | None]:
    accuracy = (stock_date_accuracy or "").strip().lower()
    if accuracy not in STOCK_DATE_ACCURACIES:
        raise WarehouseInventoryError(
            "入库日期可信度必须为 exact、estimated 或 unknown"
        )
    original = (stock_date_original_text or "").strip() or None
    if accuracy == "exact":
        if original is not None:
            try:
                parsed = date.fromisoformat(original)
            except ValueError as error:
                raise WarehouseInventoryError(
                    "精确入库日期原文必须使用 YYYY-MM-DD"
                ) from error
            if parsed != stock_date:
                raise WarehouseInventoryError(
                    "精确入库日期原文与入库日期不一致"
                )
        original = original or stock_date.isoformat()
    elif accuracy == "unknown":
        # A non-null marker distinguishes a deliberate post-migration
        # "unknown" fact from legacy rows backfilled as unknown + NULL.
        original = original or "未提供"
    return accuracy, original


def inventory_age_warning(lot: InventoryLot, *, today: date | None = None) -> AgeWarning:
    accuracy = getattr(lot, "stock_date_accuracy", "exact") or "unknown"
    if accuracy == "unknown":
        return AgeWarning(None, "unknown", "入库日期不明，不能按精确库龄判断")
    current = today or beijing_today()
    days = max((current - lot.stock_date).days, 0)
    estimated_prefix = "估算" if accuracy == "estimated" else ""
    if days >= 730:
        return AgeWarning(days, "cleanup", f"{estimated_prefix}库龄超过2年，请盘点并处理")
    if days >= 548:
        return AgeWarning(days, "handling", f"{estimated_prefix}库龄超过18个月，请安排处理")
    if days >= 365:
        return AgeWarning(days, "attention", f"{estimated_prefix}库龄超过1年，请重点关注")
    if accuracy == "estimated":
        return AgeWarning(days, "estimated", "入库日期为估算值，库龄仅供参考")
    return AgeWarning(days, None, None)


def _number(prefix: str) -> str:
    return f"{prefix}-{beijing_now_naive():%Y%m%d}-{uuid4().hex[:10].upper()}"


def _location(db: Session, location_id: int, inventory_type: str) -> WarehouseLocation:
    location = db.get(WarehouseLocation, location_id)
    allowed = {
        "finished": {"finished", "shared"},
        "semi_finished": {"semi_finished", "shared"},
    }[inventory_type]
    if location is None:
        raise WarehouseInventoryError("库位不存在", 404)
    if getattr(location, "source_version", None) == "V11":
        if inventory_type != "finished":
            raise WarehouseInventoryError(
                "三楼货位目前只接入成品仓；半成品请使用半成品库位", 409
            )
        if getattr(location, "warehouse_floor", None) != 3:
            raise WarehouseInventoryError(
                "V11 货位楼层无效，不能办理成品入库", 409
            )
    if inventory_type == "finished":
        if getattr(location, "placement_status", None) == "unplaced":
            raise WarehouseInventoryError(
                "该库位尚未完成空间放置，不能办理成品库存业务",
                409,
            )
        issue = operational_location_issue(
            db,
            location,
            warehouse_types=allowed,
        )
        if issue:
            raise WarehouseInventoryError(f"{issue}，不能办理成品库存业务", 409)
        return location
    if not location.is_active:
        raise WarehouseInventoryError("该库位已停用，不能入库")
    if getattr(location, "placement_status", None) == "unplaced":
        raise WarehouseInventoryError(
            "该库位尚未完成空间放置，不能入库；请先补齐楼层、区域和存储方式",
            409,
        )
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


def inventory_fifo_order_columns() -> tuple:
    """Order trustworthy/estimated dates first and unknown technical dates last."""
    return (
        case(
            (InventoryLot.stock_date_accuracy == "unknown", 1),
            else_=0,
        ),
        InventoryLot.stock_date,
        InventoryLot.id,
    )


def inventory_fifo_sort_key(lot: InventoryLot) -> tuple:
    """Mirror the SQL FIFO rule for lots already loaded into Python."""
    return (
        1 if (lot.stock_date_accuracy or "unknown") == "unknown" else 0,
        lot.stock_date,
        lot.id,
    )


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


def record_location_transfer_without_quantity_change(
    db: Session,
    *,
    lot: InventoryLot,
    operator_id: int | None,
    idempotency_key: str,
    remarks: str,
) -> InventoryMovement:
    """Record a physical location change without altering inventory balances."""
    before = _balances(lot)
    return _movement(
        db,
        lot=lot,
        movement_type="location_transfer",
        quantity=0,
        before=before,
        operator_id=operator_id,
        remarks=remarks,
        idempotency_key=idempotency_key,
    )


def _transfer_key(*parts: object, max_length: int = 100) -> str:
    raw = ":".join(str(part).strip() for part in parts)
    if len(raw) <= max_length:
        return raw
    digest = sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{raw[: max_length - 25]}:{digest}"


def _lot_location_transfer_hash(
    *, lot_id: int, expected_version: int, quantity: int, location_id: int
) -> str:
    payload = {
        "expected_version": expected_version,
        "location_id": location_id,
        "lot_id": lot_id,
        "quantity": quantity,
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def transfer_staging_finished_lot(
    db: Session,
    *,
    lot_id: int,
    expected_version: int,
    quantity: int,
    location_id: int,
    operator_id: int | None,
    idempotency_key: str,
) -> FinishedLotLocationTransferResult:
    """Move all or part of a floor-one staging lot without changing stock totals.

    Available pieces move first.  When the selected quantity also includes
    reserved pieces, their reservations are split onto the destination lot so
    order and delivery coverage remains unchanged.
    """

    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise WarehouseInventoryError("请求标识长度必须为1到120个字符")
    if quantity <= 0:
        raise WarehouseInventoryError("转入数量必须大于0")
    request_hash = _lot_location_transfer_hash(
        lot_id=lot_id,
        expected_version=expected_version,
        quantity=quantity,
        location_id=location_id,
    )
    repeated = db.scalar(
        select(InventoryLotTransfer).where(
            InventoryLotTransfer.idempotency_key == key
        )
    )
    if repeated is not None:
        if repeated.source_lot_id != lot_id or repeated.request_hash != request_hash:
            raise WarehouseInventoryError("同一请求标识已用于其他库位转移", 409)
        source = db.get(InventoryLot, repeated.source_lot_id)
        target = db.get(InventoryLot, repeated.target_lot_id)
        if source is None or target is None:
            raise WarehouseInventoryError("已完成的库位转移记录不完整", 409)
        return FinishedLotLocationTransferResult(repeated, source, target, True)

    lot = db.get(InventoryLot, lot_id)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError("一楼待送成品批次不存在", 404)
    source_location = db.get(WarehouseLocation, lot.warehouse_location_id)
    if (
        lot.inventory_type != "finished"
        or lot.status != "active"
        or lot.source_type not in {"production_completion", "transfer"}
        or lot.source_ref_type != "production_completion"
        or lot.source_ref_id is None
        or source_location is None
        or source_location.location_code != "F1-DISPATCH-01"
    ):
        raise WarehouseInventoryError("只有一楼待送区的有效成品批次可以转入库位", 409)
    if lot.pallet_item is not None:
        raise WarehouseInventoryError("该待送批次已绑定物理栈板，请刷新后重试", 409)
    if int(lot.version) != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    live_quantity = int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
    if quantity > live_quantity:
        raise WarehouseInventoryError(f"待送区当前只有 {live_quantity} 个可转入", 409)
    if any(
        int(value or 0) > 0
        for value in (
            lot.quantity_consumed,
            lot.quantity_damaged,
            lot.quantity_scrapped,
        )
    ):
        raise WarehouseInventoryError("该批次已有出库、报损或报废记录，不能从待送区转入", 409)

    target_location = _location(db, location_id, "finished")
    if target_location.location_code == "F1-DISPATCH-01":
        raise WarehouseInventoryError("目标库位不能仍是一楼待送区", 409)

    source_location_id = int(lot.warehouse_location_id)
    available_take = min(quantity, int(lot.quantity_available or 0))
    reserved_take = quantity - available_take
    source_before = _balances(lot)
    now = utc_now_naive()

    if quantity == live_quantity:
        updated = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.warehouse_location_id == source_location_id,
                InventoryLot.quantity_available == source_before["available"],
                InventoryLot.quantity_reserved == source_before["reserved"],
            )
            .values(
                warehouse_location_id=target_location.id,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if updated.rowcount != 1:
            raise WarehouseInventoryError("待送批次数量或版本已变化，请刷新后重试", 409)
        db.flush()
        db.expire(lot)
        target_lot = db.get(InventoryLot, lot.id)
        assert target_lot is not None
    else:
        updated = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.warehouse_location_id == source_location_id,
                InventoryLot.quantity_available >= available_take,
                InventoryLot.quantity_reserved >= reserved_take,
            )
            .values(
                quantity_available=InventoryLot.quantity_available - available_take,
                quantity_reserved=InventoryLot.quantity_reserved - reserved_take,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if updated.rowcount != 1:
            raise WarehouseInventoryError("待送批次数量或版本已变化，请刷新后重试", 409)
        detail = lot.finished_detail
        target_lot = InventoryLot(
            lot_number=_number("FG"),
            inventory_type="finished",
            warehouse_location_id=target_location.id,
            quantity_available=available_take,
            quantity_reserved=reserved_take,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit=lot.unit,
            status="active",
            source_type="transfer",
            source_ref_type=lot.source_ref_type,
            source_ref_id=lot.source_ref_id,
            stock_date=lot.stock_date,
            stock_date_accuracy=lot.stock_date_accuracy,
            stock_date_original_text=lot.stock_date_original_text,
            last_movement_at=now,
            created_by=operator_id,
            remarks=f"由一楼待送批次 {lot.lot_number} 部分转入",
            estimated_unit_cost_snapshot=lot.estimated_unit_cost_snapshot,
            estimated_square_price_snapshot=lot.estimated_square_price_snapshot,
            estimated_cost_area_m2_snapshot=lot.estimated_cost_area_m2_snapshot,
            cost_snapshot_source=lot.cost_snapshot_source,
            cost_snapshot_detail_json=lot.cost_snapshot_detail_json,
            cost_snapshot_at=lot.cost_snapshot_at,
        )
        target_lot.finished_detail = FinishedGoodsInventoryDetail(
            owner_customer_id=detail.owner_customer_id,
            owner_customer_name_snapshot=detail.owner_customer_name_snapshot,
            is_general=detail.is_general,
            product_id=detail.product_id,
            inventory_code_snapshot=detail.inventory_code_snapshot,
            product_name_snapshot=detail.product_name_snapshot,
            box_type_snapshot=detail.box_type_snapshot,
            length_mm=detail.length_mm,
            width_mm=detail.width_mm,
            height_mm=detail.height_mm,
            material_code_snapshot=detail.material_code_snapshot,
            flute_type_snapshot=detail.flute_type_snapshot,
        )
        db.add(target_lot)
        db.flush()

        remaining_reserved = reserved_take
        reservations = list(
            db.scalars(
                select(InventoryReservation)
                .where(
                    InventoryReservation.inventory_lot_id == lot.id,
                    InventoryReservation.status.in_(("active", "partial")),
                )
                .order_by(InventoryReservation.id)
            )
        )
        for reservation in reservations:
            if remaining_reserved <= 0:
                break
            remaining = (
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            take = min(remaining_reserved, remaining)
            if take <= 0:
                continue
            reservation.released_stock_quantity += take
            reservation.released_requirement_quantity += take
            reservation.released_by = operator_id
            reservation.released_at = now
            reservation.release_reason = "库存批次移动拆分"
            reservation.status = _finished_reservation_status(reservation)
            group_key = _transfer_key(
                reservation.reservation_group_key or f"reservation-{reservation.id}",
                "location-transfer",
                key,
            )
            db.add(
                InventoryReservation(
                    reservation_number=_number("RS"),
                    inventory_lot_id=target_lot.id,
                    reservation_type=reservation.reservation_type,
                    order_id=reservation.order_id,
                    order_item_id=reservation.order_item_id,
                    sales_order_item_bom_component_id=(
                        reservation.sales_order_item_bom_component_id
                    ),
                    requisition_item_id=reservation.requisition_item_id,
                    semi_requirement_id=reservation.semi_requirement_id,
                    match_rule_id=reservation.match_rule_id,
                    reserved_stock_quantity=take,
                    credited_requirement_quantity=take,
                    yield_factor=reservation.yield_factor,
                    status="active",
                    warning_codes=reservation.warning_codes,
                    warning_acknowledged_by=reservation.warning_acknowledged_by,
                    reserved_by=operator_id,
                    reserved_at=now,
                    reservation_group_key=group_key,
                    reservation_group_requested_quantity=take,
                    idempotency_key=_transfer_key("location-transfer", key, reservation.id),
                )
            )
            remaining_reserved -= take
        if remaining_reserved:
            raise WarehouseInventoryError("待送批次预占明细与库存余额不一致", 409)
        db.flush()
        db.expire(lot)
        lot = db.get(InventoryLot, lot.id)
        assert lot is not None

    if target_location.source_version == "V11":
        from app.services.floor3_locations import (
            Floor3LocationError,
            bind_finished_lot_to_floor3_pallet,
        )

        try:
            bind_finished_lot_to_floor3_pallet(
                db,
                lot=target_lot,
                operator_id=operator_id,
                require_empty_pallet=True,
            )
        except Floor3LocationError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error

    transfer = InventoryLotTransfer(
        source_lot_id=lot_id,
        target_lot_id=target_lot.id,
        source_location_id=source_location_id,
        target_location_id=target_location.id,
        quantity=quantity,
        available_quantity=available_take,
        reserved_quantity=reserved_take,
        source_version_before=expected_version,
        source_version_after=expected_version + 1,
        idempotency_key=key,
        request_hash=request_hash,
        transferred_by=operator_id,
        transferred_at=now,
    )
    db.add(transfer)
    db.flush()
    _movement(
        db,
        lot=lot,
        movement_type="location_transfer",
        quantity=quantity,
        before=source_before,
        operator_id=operator_id,
        reason="一楼待送区转入正式库位",
        remarks=f"转入 {target_location.location_code}",
        idempotency_key=_transfer_key("location-transfer", key, "source"),
    )
    if target_lot.id != lot.id:
        _movement(
            db,
            lot=target_lot,
            movement_type="location_transfer",
            quantity=quantity,
            before={
                "available": 0,
                "reserved": 0,
                "consumed": 0,
                "damaged": 0,
                "scrapped": 0,
            },
            operator_id=operator_id,
            reason="一楼待送区转入正式库位",
            remarks=f"来自 {lot.lot_number}",
            idempotency_key=_transfer_key("location-transfer", key, "target"),
        )
    db.flush()
    return FinishedLotLocationTransferResult(transfer, lot, target_lot, False)


def manual_finished_in(
    db: Session,
    *,
    customer_id: int | None,
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
    pallet_id: int | None = None,
    pallet_code: str | None = None,
    require_empty_pallet: bool = False,
    movement_reason: str = "手工成品入库",
    stock_date_accuracy: str = "exact",
    stock_date_original_text: str | None = None,
    is_general: bool = False,
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("入库数量必须大于0")
    location = _location(db, location_id, "finished")
    customer = db.get(Customer, customer_id) if customer_id is not None else None
    product = db.get(Product, product_id)
    if not is_general and customer is None:
        raise WarehouseInventoryError("客户不存在", 404)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if not is_general and product.customer_id != customer_id:
        raise WarehouseInventoryError("所选产品不属于该客户")
    date_accuracy, date_original_text = normalize_stock_date_metadata(
        stock_date=stock_date,
        stock_date_accuracy=stock_date_accuracy,
        stock_date_original_text=stock_date_original_text,
    )
    now = utc_now_naive()
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
        stock_date_accuracy=date_accuracy,
        stock_date_original_text=date_original_text,
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
        owner_customer_id=customer.id if customer is not None and not is_general else None,
        owner_customer_name_snapshot=(
            customer.name if customer is not None and not is_general else None
        ),
        is_general=is_general,
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
        reason=movement_reason,
        remarks=remarks,
        idempotency_key=idempotency_key,
    )
    if getattr(location, "source_version", None) == "V11":
        # Local import avoids a module cycle while keeping the official lot and
        # its physical-map projection in the same database transaction.
        from app.services.floor3_locations import bind_finished_lot_to_floor3_pallet

        bind_finished_lot_to_floor3_pallet(
            db,
            lot=lot,
            operator_id=operator_id,
            pallet_id=pallet_id,
            pallet_code=pallet_code,
            require_empty_pallet=require_empty_pallet,
        )
    db.flush()
    return lot


def active_finished_reserved_qty(db: Session, order_item_id: int) -> int:
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
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
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
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


def active_finished_component_reserved_qty(db: Session, snapshot_id: int) -> int:
    """Return only un-released formal finished stock bound to one BOM snapshot.

    This intentionally excludes ordinary parent-order reservations: a component
    lot must never make the parent product look like it has already been covered.
    """
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.sales_order_item_bom_component_id == snapshot_id,
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


def component_effective_required_piece_qty(
    db: Session, snapshot: SalesOrderItemBomComponent
) -> int:
    """Immutable snapshot quantity plus append-only order-specific adjustments."""
    delta = db.scalar(
        select(func.coalesce(func.sum(
            SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
        ), 0)).where(
            SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
            == snapshot.id
        )
    )
    return max(int(snapshot.required_piece_quantity or 0) + int(delta or 0), 0)


def component_inventory_coverage(
    db: Session,
    snapshot_id: int,
    *,
    component_type: str = "whole",
) -> dict[str, int]:
    """One authoritative component coverage view for reserve and requisition.

    Formal finished stock is one complete component product, so one reserved A3
    component covers one cover and one base.  Semi-finished stock is a physical
    board and must remain scoped to whole/cover/base.  Historical `whole`
    requirements continue to cover the complete legacy snapshot.
    """
    component = (component_type or "").strip().lower()
    if component not in {"whole", "cover", "base"}:
        raise WarehouseInventoryError(
            "组合组件库存类型仅允许 whole、cover 或 base"
        )
    snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
    if snapshot is None:
        raise WarehouseInventoryError("组件快照不存在", 404)
    physical_pieces_per_component = 1
    if component == "whole":
        frozen_value = int(snapshot.snapshot_component_pieces_per_box or 0)
        if frozen_value > 0:
            physical_pieces_per_component = frozen_value
        elif (
            snapshot.snapshot_component_splice_mode or ""
        ).strip().lower() == "double":
            physical_pieces_per_component = 2
    finished = (
        active_finished_component_reserved_qty(db, snapshot_id)
        * physical_pieces_per_component
    )
    # `semi_requirement_id` was the only link before the explicit snapshot
    # field existed. Keep that historical link in the one coverage view so an
    # older, still-active reservation cannot be counted a second time by a
    # later finished-goods reservation.
    semi_rows = db.scalars(
        select(InventoryReservation)
        .outerjoin(
            OrderItemSemiRequirement,
            OrderItemSemiRequirement.id == InventoryReservation.semi_requirement_id,
        )
        .where(
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
            (
                or_(
                    OrderItemSemiRequirement.id.is_(None),
                    OrderItemSemiRequirement.component_type == "whole",
                )
                if component == "whole"
                else or_(
                    OrderItemSemiRequirement.id.is_(None),
                    OrderItemSemiRequirement.component_type.in_(
                        [component, "whole"]
                    ),
                )
            ),
            or_(
                InventoryReservation.sales_order_item_bom_component_id == snapshot_id,
                OrderItemSemiRequirement.sales_order_item_bom_component_id
                == snapshot_id,
            ),
        )
    ).all()
    semi = sum(
        max(
            int(row.credited_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
        for row in semi_rows
    )
    return {
        "finished_piece_quantity": finished,
        "semi_piece_quantity": semi,
        "total_piece_quantity": finished + semi,
    }


def finished_inventory_candidates_for_bom_component(
    db: Session, *, order_item_id: int, bom_snapshot_id: int
) -> list[InventoryLot]:
    """Candidates for an internal component, limited to the owning customer.

    General stock is included solely so the caller can display its explicit
    warning; it is never silently selected by the reservation service.
    """
    snapshot = db.get(SalesOrderItemBomComponent, bom_snapshot_id)
    item = db.get(OrderItem, order_item_id)
    if snapshot is None or item is None or snapshot.sales_order_item_id != item.id:
        raise WarehouseInventoryError("组件快照不属于当前订单明细", 409)
    order = db.get(Order, item.order_id)
    if order is None:
        raise WarehouseInventoryError("订单不存在", 404)
    return db.scalars(
        select(InventoryLot)
        .join(FinishedGoodsInventoryDetail,
              FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id)
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            FinishedGoodsInventoryDetail.product_id == snapshot.component_product_id,
            or_(
                FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
                FinishedGoodsInventoryDetail.is_general.is_(True),
            ),
        )
        .order_by(FinishedGoodsInventoryDetail.is_general, InventoryLot.stock_date, InventoryLot.id)
    ).all()


def reserve_finished_inventory_for_bom_component(
    db: Session,
    *,
    order_item_id: int,
    bom_snapshot_id: int,
    inventory_lot_id: int,
    quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    warning_acknowledged_codes: list[str],
) -> InventoryReservation:
    """Reserve formal component stock without changing parent-order coverage."""
    existing = db.scalar(select(InventoryReservation).where(
        InventoryReservation.idempotency_key == idempotency_key
    ))
    if existing is not None:
        if (
            existing.order_item_id != order_item_id
            or existing.inventory_lot_id != inventory_lot_id
            or existing.sales_order_item_bom_component_id != bom_snapshot_id
            or int(existing.reserved_stock_quantity or 0) != quantity
        ):
            raise WarehouseInventoryError("该请求标识已用于其他组件库存预占", 409)
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("组件成品库存抵扣数量必须大于0")
    row = db.execute(select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id)
                     .where(OrderItem.id == order_item_id)).one_or_none()
    snapshot = db.get(SalesOrderItemBomComponent, bom_snapshot_id)
    if row is None or snapshot is None or snapshot.sales_order_item_id != order_item_id:
        raise WarehouseInventoryError("组件快照不属于当前订单明细", 409)
    item, order = row
    from app.services.production_workflow import has_production_completion_facts, lock_order_rows_for_production_transition, ProductionWorkflowError
    try:
        lock_order_rows_for_production_transition(db, [order.id])
    except ProductionWorkflowError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error
    if item.requisition_status != "未报料" or item.material_status == "received" or item.delivered_quantity > 0:
        raise WarehouseInventoryError("订单已进入后续流程，不能新增组件成品库存抵扣", 409)
    if has_production_completion_facts(db, [item.id]):
        raise WarehouseInventoryError("订单明细已完成生产，不能再新增组件成品库存抵扣", 409)
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.finished_detail is None or lot.inventory_type != "finished" or lot.status != "active":
        raise WarehouseInventoryError("组件成品库存批次当前不可预占", 409)
    detail = lot.finished_detail
    if detail.product_id != snapshot.component_product_id:
        raise WarehouseInventoryError("库存产品与组件不一致", 409)
    warnings: list[str] = []
    if detail.is_general:
        warnings.append("GENERAL_FINISHED_STOCK")
        if "GENERAL_FINISHED_STOCK" not in warning_acknowledged_codes:
            raise WarehouseInventoryError("通用组件成品库存必须人工确认后才能使用", 409)
    elif detail.owner_customer_id != order.customer_id:
        raise WarehouseInventoryError("客户专用组件库存不能用于其他客户订单", 409)
    physical_pieces_per_component = int(
        snapshot.snapshot_component_pieces_per_box or 0
    )
    if physical_pieces_per_component <= 0:
        physical_pieces_per_component = (
            2
            if (snapshot.snapshot_component_splice_mode or "")
            .strip()
            .lower()
            == "double"
            else 1
        )
    coverage = component_inventory_coverage(db, bom_snapshot_id)
    remaining_physical_pieces = max(
        component_effective_required_piece_qty(db, snapshot)
        * physical_pieces_per_component
        - int(coverage["total_piece_quantity"]),
        0,
    )
    remaining_component_units = (
        remaining_physical_pieces // physical_pieces_per_component
    )
    if quantity > remaining_component_units:
        raise WarehouseInventoryError(
            f"组件成品抵扣数量不能超过剩余 {remaining_component_units}",
            409,
        )
    if lot.version != expected_version or quantity > int(lot.quantity_available or 0):
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
    before = _balances(lot)
    now = utc_now_naive()
    result = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot.id, InventoryLot.version == expected_version,
        InventoryLot.quantity_available >= quantity, InventoryLot.inventory_type == "finished",
        InventoryLot.status == "active",
    ).values(quantity_available=InventoryLot.quantity_available - quantity,
             quantity_reserved=InventoryLot.quantity_reserved + quantity,
             version=InventoryLot.version + 1, last_movement_at=now))
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
    reservation = InventoryReservation(
        reservation_number=_number("BRS"), inventory_lot_id=lot.id,
        reservation_type="finished_order", order_id=order.id, order_item_id=item.id,
        sales_order_item_bom_component_id=snapshot.id,
        reserved_stock_quantity=quantity, credited_requirement_quantity=quantity,
        yield_factor=1, status="active", warning_codes=json.dumps(warnings, ensure_ascii=False),
        warning_acknowledged_by=operator_id if warnings else None,
        reserved_by=operator_id, reserved_at=now, idempotency_key=idempotency_key,
    )
    db.add(reservation); db.flush(); db.expire(lot)
    refreshed = db.get(InventoryLot, lot.id)
    assert refreshed is not None
    _movement(db, lot=refreshed, movement_type="reserve", quantity=quantity, before=before,
              operator_id=operator_id, reason="组合 BOM 组件成品库存抵扣",
              idempotency_key=idempotency_key, reservation_id=reservation.id,
              related_order_id=order.id, related_order_item_id=item.id)
    db.flush()
    return reservation


def finished_inventory_candidates(db: Session, order_item_id: int) -> list[InventoryLot]:
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    from app.services.production_workflow import has_production_completion_facts

    if has_production_completion_facts(db, [item.id]):
        raise WarehouseInventoryError(
            "订单明细已完成生产，不能再新增成品库存抵扣", 409
        )
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
            *inventory_fifo_order_columns(),
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
        .order_by(*inventory_fifo_order_columns())
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
    # Use the same order-row lock as production completion and order terminal
    # transitions, then re-read under that lock before creating a reservation.
    from app.services.production_workflow import (
        ProductionWorkflowError,
        has_production_completion_facts,
        lock_order_rows_for_production_transition,
    )

    try:
        lock_order_rows_for_production_transition(db, [order.id])
    except ProductionWorkflowError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在或已被删除", 409)
    item, order = row
    if has_production_completion_facts(db, [item.id]):
        raise WarehouseInventoryError(
            "订单明细已完成生产，不能再新增成品库存抵扣", 409
        )
    if item.requisition_status != "未报料":
        raise WarehouseInventoryError("订单已进入报料，请先取消报料后再抵扣成品库存", 409)
    if item.material_status == "received" or item.delivered_quantity > 0:
        raise WarehouseInventoryError("订单明细已进入后续流程，不能新增成品库存抵扣", 409)
    active_semi_reservation = db.scalar(
        select(InventoryReservation.id)
        .where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .limit(1)
    )
    if active_semi_reservation is not None:
        raise WarehouseInventoryError(
            "订单已有半成品库存预占，请先取消半成品抵扣后再改用成品库存",
            409,
        )
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
    now = utc_now_naive()
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
    # N029 tasks are optional for legacy orders.  Refresh only an existing
    # task after the reservation write has succeeded.
    from app.services.production_workflow import refresh_existing_production_task

    refresh_existing_production_task(db, item.id)
    return reservation


def reserve_completed_finished_inventory(
    db: Session,
    *,
    order_item_id: int,
    inventory_lot_id: int,
    quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
) -> InventoryReservation:
    """Reserve a production-completion lot for its original order item.

    This is intentionally separate from the operator-facing reservation flow:
    production may complete after requisition or material receipt, but every
    ownership, product, quantity, version and idempotency invariant still
    applies.
    """
    existing = db.scalar(
        select(InventoryReservation).where(
            InventoryReservation.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.reservation_type != "finished_order"
            or existing.order_item_id != order_item_id
            or existing.inventory_lot_id != inventory_lot_id
            or int(existing.reserved_stock_quantity or 0) != quantity
            or int(existing.credited_requirement_quantity or 0) != quantity
        ):
            raise WarehouseInventoryError("该请求标识已用于其他库存预占", 409)
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("生产完工预占数量必须大于0")
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError("生产完工成品库存批次不存在", 404)
    detail = lot.finished_detail
    if (
        lot.inventory_type != "finished"
        or lot.status != "active"
        or lot.source_type not in {"production_surplus", "production_completion"}
        or lot.source_ref_type != "production_completion"
        or lot.source_ref_id is None
    ):
        raise WarehouseInventoryError("该库存批次不是有效的生产完工入库", 409)
    from app.models.production import ProductionCompletion

    completion = db.get(ProductionCompletion, lot.source_ref_id)
    if (
        completion is None
        or completion.order_item_id != item.id
        or int(
            completion.actual_output_quantity
            if lot.source_type == "production_completion"
            else (completion.stock_quantity or completion.quantity or 0)
        )
        != int(lot.quantity_available or 0)
    ):
        raise WarehouseInventoryError("完工库存与生产完工事实不一致", 409)
    if detail.product_id != item.product_id:
        raise WarehouseInventoryError("完工库存产品与订单产品不一致", 409)
    if detail.is_general or detail.owner_customer_id != order.customer_id:
        raise WarehouseInventoryError("完工库存客户与订单客户不一致", 409)
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    if int(lot.quantity_available or 0) < quantity:
        raise WarehouseInventoryError("完工库存不足以建立订单预占", 409)
    before = _balances(lot)
    now = utc_now_naive()
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
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
    reservation = InventoryReservation(
        reservation_number=_number("PRS"),
        inventory_lot_id=lot.id,
        reservation_type="finished_order",
        order_id=order.id,
        order_item_id=item.id,
        reserved_stock_quantity=quantity,
        credited_requirement_quantity=quantity,
        yield_factor=1,
        status="active",
        warning_codes="[]",
        reserved_by=operator_id,
        reserved_at=now,
        idempotency_key=idempotency_key,
    )
    db.add(reservation)
    db.flush()
    db.expire(lot)
    refreshed_lot = db.get(InventoryLot, lot.id)
    assert refreshed_lot is not None
    _movement(
        db,
        lot=refreshed_lot,
        movement_type="reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason="生产完工自动预占",
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=order.id,
        related_order_item_id=item.id,
    )
    db.flush()
    return reservation


def reserve_finished_surplus_for_delivery(
    db: Session,
    *,
    order_item_id: int,
    quantity: int,
    operator_id: int | None,
    operation_key: str,
) -> list[InventoryReservation]:
    """Atomically reserve customer-owned surplus solely for over-delivery.

    These reservations never credit the order requirement and therefore are
    excluded from normal order coverage.  They exist only to make the
    additional physical stock consumption auditable and reversible.
    """
    if quantity <= 0:
        return []
    prefix = f"{operation_key}-surplus-"
    repeated = db.scalars(
        select(InventoryReservation)
        .where(
            InventoryReservation.idempotency_key.like(f"{prefix}%"),
            InventoryReservation.reservation_type == "finished_surplus_delivery",
            InventoryReservation.order_item_id == order_item_id,
        )
        .order_by(InventoryReservation.id)
    ).all()
    if repeated:
        if sum(int(row.reserved_stock_quantity) for row in repeated) != quantity:
            raise WarehouseInventoryError("超量送货库存幂等内容不一致", 409)
        return list(repeated)
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    lots = db.scalars(
        select(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            InventoryLot.source_type.in_(
                ("production_surplus", "production_completion", "transfer")
            ),
            FinishedGoodsInventoryDetail.product_id == item.product_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
        )
        .order_by(
            case(
                (
                    (InventoryLot.source_ref_type == "production_completion")
                    & (
                        InventoryLot.source_ref_id.in_(
                            select(ProductionCompletion.id).where(
                                ProductionCompletion.order_item_id == item.id,
                                ProductionCompletion.status == "posted",
                            )
                        )
                    ),
                    0,
                ),
                else_=1,
            ),
            *inventory_fifo_order_columns(),
        )
    ).all()
    remaining = quantity
    reservations: list[InventoryReservation] = []
    now = utc_now_naive()
    for lot in lots:
        if remaining <= 0:
            break
        take = min(int(lot.quantity_available or 0), remaining)
        if take <= 0:
            continue
        before = _balances(lot)
        expected_version = int(lot.version)
        updated = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_available >= take,
            )
            .values(
                quantity_available=InventoryLot.quantity_available - take,
                quantity_reserved=InventoryLot.quantity_reserved + take,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if updated.rowcount != 1:
            raise WarehouseInventoryError("余货库存已变化，请刷新后重试", 409)
        reservation = InventoryReservation(
            reservation_number=_number("ODS"),
            inventory_lot_id=lot.id,
            reservation_type="finished_surplus_delivery",
            order_id=order.id,
            order_item_id=item.id,
            reserved_stock_quantity=take,
            credited_requirement_quantity=take,
            yield_factor=1,
            status="active",
            warning_codes="[]",
            reserved_by=operator_id,
            reserved_at=now,
            idempotency_key=f"{prefix}{lot.id}",
        )
        db.add(reservation)
        db.flush()
        db.expire(lot)
        refreshed = db.get(InventoryLot, lot.id)
        assert refreshed is not None
        _movement(
            db,
            lot=refreshed,
            movement_type="reserve",
            quantity=take,
            before=before,
            operator_id=operator_id,
            reason="授权超量送货预占客户专用余货",
            idempotency_key=f"{prefix}{lot.id}",
            reservation_id=reservation.id,
            related_order_id=order.id,
            related_order_item_id=item.id,
        )
        reservations.append(reservation)
        remaining -= take
    if remaining > 0:
        raise WarehouseInventoryError(
            f"客户专用成品余货不足，超量送货还缺 {remaining}",
            409,
        )
    return reservations


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
        if reservation.reservation_type not in {
            "finished_order",
            "finished_surplus_delivery",
        }:
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
        now = utc_now_naive()
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
        if reservation is None or reservation.reservation_type not in {
            "finished_order",
            "finished_surplus_delivery",
        }:
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
        now = utc_now_naive()
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
    release_reason: str | None,
    idempotency_key: str,
    allow_downstream: bool = False,
    allow_production_reversal: bool = False,
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
    lot = db.get(InventoryLot, reservation.inventory_lot_id)
    if lot is None:
        raise WarehouseInventoryError("关联库存批次不存在", 409)
    if lot.source_ref_type == "production_completion" and not allow_production_reversal:
        raise WarehouseInventoryError(
            "生产完工自动预占属于完工事实，当前不允许手工释放", 409
        )
    from app.services.production_workflow import has_production_completion_facts

    if not allow_production_reversal and reservation.order_item_id is not None and has_production_completion_facts(
        db, [reservation.order_item_id]
    ):
        raise WarehouseInventoryError(
            "该订单明细已有生产完工事实，不能释放成品预占", 409
        )
    remaining = (
        int(reservation.reserved_stock_quantity)
        - int(reservation.consumed_stock_quantity or 0)
        - int(reservation.released_stock_quantity or 0)
    )
    if remaining <= 0:
        raise WarehouseInventoryError("该预占已释放或已消耗，不能重复释放", 409)
    reason = (release_reason or "").strip() or "取消成品库存抵扣（系统记录）"
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
    quantity = remaining
    if lot.quantity_reserved < quantity:
        raise WarehouseInventoryError("库存预占余额异常，请联系管理员处理", 409)
    before = _balances(lot)
    now = utc_now_naive()
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
    from app.services.production_workflow import refresh_existing_production_task

    refresh_existing_production_task(db, reservation.order_item_id)
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
    movement_reason: str = "手工半成品入库",
    stock_date_accuracy: str = "exact",
    stock_date_original_text: str | None = None,
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
    material = db.get(Material, material_id) if material_id else None
    if material_id and (material is None or not material.is_active):
        raise WarehouseInventoryError("材质主数据不存在或已停用", 404)
    if material is not None:
        material_code = material.code
        if material.layer_count is not None:
            layer_count = material.layer_count
    code_error = seven_layer_code_error(material_code, layer_count)
    if code_error:
        raise WarehouseInventoryError(code_error)
    flute = flute_type.strip().upper()
    if (
        layer_count not in SEMI_FINISHED_FLUTES_BY_LAYER
        or flute not in SEMI_FINISHED_FLUTES_BY_LAYER[layer_count]
    ):
        raise WarehouseInventoryError(
            "三层仅支持A/B/E楞，五层仅支持AB/BE楞，七层仅支持AAA/ABC楞"
        )
    if sheet_type not in {"raw_board", "net_sheet", "creased_sheet"}:
        raise WarehouseInventoryError("片料类型无效")
    _location(db, location_id, "semi_finished")
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer_id and customer is None:
        raise WarehouseInventoryError("客户不存在", 404)
    date_accuracy, date_original_text = normalize_stock_date_metadata(
        stock_date=stock_date,
        stock_date_accuracy=stock_date_accuracy,
        stock_date_original_text=stock_date_original_text,
    )
    now = utc_now_naive()
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
        stock_date_accuracy=date_accuracy,
        stock_date_original_text=date_original_text,
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
        reason=movement_reason,
        remarks=remarks,
        idempotency_key=idempotency_key,
    )
    db.flush()
    return lot


SEMI_FINISHED_ASSIGN_CUSTOMER_REASON = "指定半成品归属客户"
SEMI_FINISHED_UNASSIGN_CUSTOMER_REASON = "取消半成品归属客户"
SEMI_FINISHED_VOID_REASON = "作废误录半成品库存批次"


def _editable_semi_finished_lot(
    db: Session, lot_id: int, expected_version: int
) -> InventoryLot:
    lot = db.get(InventoryLot, lot_id)
    if lot is None or lot.inventory_type != "semi_finished" or lot.semi_finished_detail is None:
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)
    if lot.status == "closed":
        raise WarehouseInventoryError("已关闭的半成品库存批次不能编辑", 409)
    return lot


def semi_finished_lot_allowed_product_ids(
    db: Session, inventory_lot_id: int
) -> tuple[int, ...]:
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.inventory_type != "semi_finished":
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    return tuple(
        db.scalars(
            select(SemiFinishedLotAllowedProduct.product_id)
            .where(SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id)
            .order_by(SemiFinishedLotAllowedProduct.product_id)
        ).all()
    )


def _lot_product_binding_has_blocking_usage(
    db: Session,
    *,
    lot_id: int,
    product_id: int | None = None,
) -> bool:
    filters = [InventoryReservation.inventory_lot_id == lot_id]
    if product_id is not None:
        filters.append(InventoryReservation.order_item_id == OrderItem.id)
        filters.append(OrderItem.product_id == product_id)
    return db.scalar(
        select(InventoryReservation.id)
        .select_from(InventoryReservation)
        .join(
            OrderItem,
            OrderItem.id == InventoryReservation.order_item_id,
            isouter=product_id is None,
        )
        .where(
            *filters,
            or_(
                InventoryReservation.reserved_stock_quantity
                > InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity,
                InventoryReservation.consumed_stock_quantity > 0,
            ),
        )
        .limit(1)
    ) is not None


def replace_semi_finished_lot_allowed_products(
    db: Session,
    *,
    inventory_lot_id: int,
    product_ids: list[int],
    expected_version: int,
    operator_id: int | None,
) -> InventoryLot:
    """Replace one lot's hard bindings without changing shared match memory."""

    lot = _editable_semi_finished_lot(db, inventory_lot_id, expected_version)
    detail = lot.semi_finished_detail
    assert detail is not None
    if detail.owner_customer_id is None:
        raise WarehouseInventoryError("请先为半成品库存指定归属客户", 409)

    desired = set(dict.fromkeys(int(value) for value in product_ids))
    products = (
        db.scalars(
            select(Product).where(
                Product.id.in_(desired),
                Product.customer_id == detail.owner_customer_id,
                Product.is_active.is_(True),
                Product.deleted_at.is_(None),
            )
        ).all()
        if desired
        else []
    )
    if len(products) != len(desired):
        raise WarehouseInventoryError(
            "半成品库存只能绑定同一客户的启用成品款号", 409
        )

    current = set(
        db.scalars(
            select(SemiFinishedLotAllowedProduct.product_id).where(
                SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id
            )
        ).all()
    )
    removed = current - desired
    for product_id in removed:
        if _lot_product_binding_has_blocking_usage(
            db, lot_id=lot.id, product_id=product_id
        ):
            raise WarehouseInventoryError(
                "该款号已有活跃预占或净消耗，不能移除批次硬绑定", 409
            )
    if current == desired:
        return lot

    now = utc_now_naive()
    updated = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.version == expected_version,
        )
        .values(version=expected_version + 1, last_movement_at=now)
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)
    if removed:
        db.execute(
            delete(SemiFinishedLotAllowedProduct).where(
                SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id,
                SemiFinishedLotAllowedProduct.product_id.in_(removed),
            )
        )
    for product_id in desired - current:
        db.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=lot.id,
                product_id=product_id,
                confirmed_by=operator_id,
                confirmed_at=now,
            )
        )
    db.flush()
    db.expire_all()
    refreshed = db.get(InventoryLot, lot.id)
    assert refreshed is not None
    return refreshed


def edit_semi_finished_lot_customer(
    db: Session, *, lot_id: int, customer_id: int | None,
    expected_version: int, operator_id: int | None,
) -> InventoryLot:
    lot = _editable_semi_finished_lot(db, lot_id, expected_version)
    detail = lot.semi_finished_detail
    assert detail is not None
    current_customer_id = detail.owner_customer_id
    if current_customer_id is None:
        if customer_id is None:
            raise WarehouseInventoryError("当前批次尚未指定客户，无需取消归属", 409)
        if (
            lot.quantity_reserved
            or lot.quantity_consumed
            or _lot_product_binding_has_blocking_usage(db, lot_id=lot.id)
        ):
            raise WarehouseInventoryError(
                "批次已有预占或消耗记录，不能指定客户归属", 409
            )
        customer = db.get(Customer, customer_id)
        if customer is None or not customer.is_active or customer.status != "active":
            raise WarehouseInventoryError("客户不存在或已停用", 404)
        next_customer_id = customer.id
        next_customer_name = customer.name
        reason = SEMI_FINISHED_ASSIGN_CUSTOMER_REASON
    else:
        if customer_id is not None:
            if customer_id == current_customer_id:
                raise WarehouseInventoryError("半成品库存批次归属客户未变化", 409)
            raise WarehouseInventoryError("如需更换客户，请先取消当前归属，再重新指定客户", 409)
        if (
            lot.quantity_reserved
            or lot.quantity_consumed
            or _lot_product_binding_has_blocking_usage(db, lot_id=lot.id)
        ):
            raise WarehouseInventoryError(
                "批次已有预占或消耗记录，不能取消客户归属", 409
            )
        next_customer_id = None
        next_customer_name = None
        reason = SEMI_FINISHED_UNASSIGN_CUSTOMER_REASON

    before = _balances(lot)
    now = utc_now_naive()
    updated_lot = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot_id,
        InventoryLot.inventory_type == "semi_finished",
        InventoryLot.version == expected_version,
    ).values(version=expected_version + 1, last_movement_at=now).execution_options(
        synchronize_session=False
    ))
    if updated_lot.rowcount != 1:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)
    detail.owner_customer_id = next_customer_id
    detail.owner_customer_name_snapshot = next_customer_name
    if next_customer_id is None:
        db.execute(
            delete(SemiFinishedLotAllowedProduct).where(
                SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id
            )
        )
    db.flush()
    db.expire_all()
    refreshed = db.get(InventoryLot, lot_id)
    assert refreshed is not None
    _movement(
        db, lot=refreshed, movement_type="adjust", quantity=0, before=before,
        operator_id=operator_id, reason=reason, remarks=json.dumps({
            "before_customer_id": current_customer_id,
            "after_customer_id": next_customer_id,
        }, ensure_ascii=False),
    )
    db.flush()
    return refreshed


def void_semi_finished_lot(
    db: Session, *, lot_id: int, expected_version: int,
    reason: str | None, operator_id: int | None,
) -> InventoryLot:
    normalized_reason = (reason or "").strip() or "删除误录半成品批次（系统记录）"
    lot = _editable_semi_finished_lot(db, lot_id, expected_version)
    if any((lot.quantity_reserved, lot.quantity_consumed, lot.quantity_damaged, lot.quantity_scrapped)):
        raise WarehouseInventoryError("批次已有预占、消耗、报损或报废，不能删除", 409)
    if lot.source_ref_type is not None or lot.source_ref_id is not None:
        raise WarehouseInventoryError("批次关联来料或补库业务，不能删除", 409)
    reservation_id = db.scalar(select(InventoryReservation.id).where(
        InventoryReservation.inventory_lot_id == lot.id
    ).limit(1))
    if reservation_id is not None:
        raise WarehouseInventoryError("批次已有订单或报料预占记录，不能删除", 409)
    before = _balances(lot)
    now = utc_now_naive()
    audit_note = f"作废原因：{normalized_reason}"
    lot_remarks = "\n".join(part for part in ((lot.remarks or "").strip(), audit_note) if part)
    updated = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot.id,
        InventoryLot.inventory_type == "semi_finished",
        InventoryLot.version == expected_version,
        InventoryLot.status.in_(("active", "frozen")),
    ).values(
        quantity_available=0, status="closed", version=expected_version + 1,
        last_movement_at=now, remarks=lot_remarks,
    ).execution_options(synchronize_session=False))
    if updated.rowcount != 1:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)

    db.expire_all()
    refreshed = db.get(InventoryLot, lot.id)
    assert refreshed is not None
    _movement(
        db, lot=refreshed, movement_type="adjust", quantity=before["available"],
        before=before, operator_id=operator_id,
        reason=f"{SEMI_FINISHED_VOID_REASON}：{normalized_reason}",
        remarks="保留原始入库流水并关闭批次",
    )
    db.flush()
    return refreshed


FINISHED_LOT_EDIT_REASON = "编辑成品库存批次"


def _finished_lot_edit_request(
    *,
    expected_version: int,
    is_general: bool,
    customer_id: int | None,
    product_id: int,
    quantity_available: int,
    location_id: int,
    stock_date: date,
    confirm_stock_date_exact: bool,
) -> dict[str, object]:
    return {
        "expected_version": expected_version,
        "is_general": is_general,
        "customer_id": customer_id,
        "product_id": product_id,
        "quantity_available": quantity_available,
        "location_id": location_id,
        "stock_date": stock_date.isoformat(),
        "confirm_stock_date_exact": confirm_stock_date_exact,
    }


def _idempotent_finished_lot_edit(
    db: Session,
    *,
    lot_id: int,
    idempotency_key: str,
    request_payload: dict[str, object],
) -> InventoryLot | None:
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if movement is None:
        return None
    try:
        audit = json.loads(movement.remarks or "")
    except (TypeError, json.JSONDecodeError):
        audit = None
    if (
        movement.inventory_lot_id != lot_id
        or movement.movement_type != "adjust"
        or movement.reason != FINISHED_LOT_EDIT_REASON
        or not isinstance(audit, dict)
        or audit.get("action") != FINISHED_LOT_EDIT_REASON
        or audit.get("request") != request_payload
    ):
        raise WarehouseInventoryError("幂等键已用于不同的库存业务", 409)
    lot = db.get(InventoryLot, lot_id)
    if lot is None:
        raise WarehouseInventoryError("库存批次不存在", 404)
    return lot


def _finished_product_snapshot(
    db: Session,
    *,
    product_id: int,
    is_general: bool,
    customer_id: int | None,
) -> tuple[Product, Customer, str | None]:
    product = db.get(Product, product_id)
    if product is None or not product.is_active or product.deleted_at is not None:
        raise WarehouseInventoryError("所选产品不存在或已停用", 409)
    if not is_general:
        if customer_id is None:
            raise WarehouseInventoryError("客户专用库存必须选择客户")
        if product.customer_id != customer_id:
            raise WarehouseInventoryError("所选产品不属于当前客户", 409)
    customer = db.get(Customer, product.customer_id)
    if customer is None:
        raise WarehouseInventoryError("所选产品缺少有效客户", 409)
    material_code = (
        product.material.code
        if product.material is not None
        else product.default_material_code
    ) or product.legacy_material_text
    return product, customer, material_code


def _edit_change(
    changes: dict[str, dict[str, object]],
    field: str,
    before: object,
    after: object,
) -> None:
    if before != after:
        changes[field] = {"before": before, "after": after}


def edit_finished_lot(
    db: Session,
    *,
    lot_id: int,
    expected_version: int,
    is_general: bool,
    customer_id: int | None,
    product_id: int,
    quantity_available: int,
    location_id: int,
    stock_date: date,
    operator_id: int | None,
    idempotency_key: str,
    confirm_stock_date_exact: bool = False,
) -> InventoryLot:
    """Edit one formal finished-goods lot and its physical projection atomically."""
    idempotency_key = idempotency_key.strip()
    if not idempotency_key:
        raise WarehouseInventoryError("幂等键不能为空")
    if quantity_available < 0:
        raise WarehouseInventoryError("可用库存不能小于0")
    request_payload = _finished_lot_edit_request(
        expected_version=expected_version,
        is_general=is_general,
        customer_id=customer_id,
        product_id=product_id,
        quantity_available=quantity_available,
        location_id=location_id,
        stock_date=stock_date,
        confirm_stock_date_exact=confirm_stock_date_exact,
    )
    existing = _idempotent_finished_lot_edit(
        db,
        lot_id=lot_id,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )
    if existing is not None:
        return existing

    lot = db.get(InventoryLot, lot_id)
    if (
        lot is None
        or lot.inventory_type != "finished"
        or lot.finished_detail is None
    ):
        raise WarehouseInventoryError("成品库存批次不存在", 404)
    if lot.version != expected_version:
        raise WarehouseInventoryError(
            "库存已被其他人修改，请刷新后重试", 409
        )
    if lot.status != "active":
        raise WarehouseInventoryError("只有正常状态的成品库存可以编辑", 409)

    target_location = _location(db, location_id, "finished")
    product, product_customer, material_code = _finished_product_snapshot(
        db,
        product_id=product_id,
        is_general=is_general,
        customer_id=customer_id,
    )
    detail = lot.finished_detail
    owner_customer_id = None if is_general else product_customer.id
    owner_customer_name = product_customer.name
    identity_changed = (
        detail.product_id != product.id
        or detail.owner_customer_id != owner_customer_id
        or detail.is_general != is_general
    )
    if identity_changed and (
        lot.quantity_reserved > 0 or lot.quantity_consumed > 0
    ):
        raise WarehouseInventoryError(
            "批次已有预占或消耗，不能修改产品、客户或通用归属", 409
        )
    target_snapshots: dict[str, object] = {
        "inventory_code_snapshot": product.product_code,
        "product_name_snapshot": product.product_name,
        "box_type_snapshot": product.box_style,
        "length_mm": round(product.length_mm) if product.length_mm is not None else None,
        "width_mm": round(product.width_mm) if product.width_mm is not None else None,
        "height_mm": round(product.height_mm) if product.height_mm is not None else None,
        "material_code_snapshot": material_code,
        "flute_type_snapshot": product.flute_type,
    }

    changes: dict[str, dict[str, object]] = {}
    _edit_change(changes, "is_general", detail.is_general, is_general)
    _edit_change(
        changes,
        "owner_customer_id",
        detail.owner_customer_id,
        owner_customer_id,
    )
    _edit_change(
        changes,
        "owner_customer_name_snapshot",
        detail.owner_customer_name_snapshot,
        owner_customer_name,
    )
    _edit_change(changes, "product_id", detail.product_id, product.id)
    for field, value in target_snapshots.items():
        _edit_change(changes, field, getattr(detail, field), value)
    _edit_change(
        changes,
        "quantity_available",
        lot.quantity_available,
        quantity_available,
    )
    _edit_change(
        changes,
        "warehouse_location_id",
        lot.warehouse_location_id,
        target_location.id,
    )
    _edit_change(
        changes,
        "stock_date",
        lot.stock_date.isoformat(),
        stock_date.isoformat(),
    )
    stock_date_changed = lot.stock_date != stock_date
    stock_date_confirmed = stock_date_changed or confirm_stock_date_exact
    if stock_date_confirmed:
        _edit_change(
            changes,
            "stock_date_accuracy",
            lot.stock_date_accuracy,
            "exact",
        )
        _edit_change(
            changes,
            "stock_date_original_text",
            lot.stock_date_original_text,
            stock_date.isoformat(),
        )

    before = _balances(lot)
    location_changed = lot.warehouse_location_id != target_location.id
    pallet_item = lot.pallet_item
    pallet = pallet_item.pallet if pallet_item is not None else None
    pallet_moved = False
    if pallet_item is not None:
        if pallet is None or not pallet.is_current or pallet.location_id is None:
            raise WarehouseInventoryError("成品库存绑定的真实栈板状态无效", 409)
        if pallet.location_id != lot.warehouse_location_id:
            raise WarehouseInventoryError("成品库存与真实栈板货位不一致", 409)

    if location_changed and target_location.source_version == "V11":
        occupied = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.location_id == target_location.id,
                InventoryPallet.is_current.is_(True),
            )
        )
        if occupied is not None and (pallet is None or occupied.id != pallet.id):
            raise WarehouseInventoryError("目标三楼货位已被其它真实栈板占用", 409)

    now = utc_now_naive()
    stock_date_values = (
        {
            "stock_date_accuracy": "exact",
            "stock_date_original_text": stock_date.isoformat(),
        }
        if stock_date_confirmed
        else {}
    )
    if location_changed and pallet is not None:
        from app.services.floor3_locations import Floor3LocationError, move_pallet

        move_key = f"finished-edit:{sha256(idempotency_key.encode('utf-8')).hexdigest()}"
        try:
            move_pallet(
                db,
                pallet_id=pallet.id,
                expected_version=pallet.version,
                to_location_id=target_location.id,
                remarks=FINISHED_LOT_EDIT_REASON,
                operator_id=operator_id,
                idempotency_key=move_key,
            )
        except Floor3LocationError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error
        pallet_moved = True
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version + 1,
                InventoryLot.warehouse_location_id == target_location.id,
            )
            .values(
                quantity_available=quantity_available,
                stock_date=stock_date,
                last_movement_at=now,
                **stock_date_values,
            )
        )
    else:
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
            )
            .values(
                quantity_available=quantity_available,
                warehouse_location_id=target_location.id,
                stock_date=stock_date,
                version=expected_version + 1,
                last_movement_at=now,
                **stock_date_values,
            )
        )
    if result.rowcount != 1:
        raise WarehouseInventoryError(
            "库存已被其他人修改，请刷新后重试", 409
        )

    detail.owner_customer_id = owner_customer_id
    detail.owner_customer_name_snapshot = owner_customer_name
    detail.is_general = is_general
    detail.product_id = product.id
    for field, value in target_snapshots.items():
        setattr(detail, field, value)

    if pallet_item is not None:
        projection_values = {
            "customer_id": product_customer.id,
            "product_id": product.id,
            "inventory_code": product.product_code,
            "customer_name_snapshot": product_customer.name,
            "product_name": product.product_name,
            "quantity": max(quantity_available + lot.quantity_reserved, 1),
            "match_status": "matched",
        }
        projection_changed = any(
            getattr(pallet_item, field) != value
            for field, value in projection_values.items()
        )
        for field, value in projection_values.items():
            setattr(pallet_item, field, value)
        if projection_changed and not pallet_moved and pallet is not None:
            pallet_result = db.execute(
                update(InventoryPallet)
                .where(
                    InventoryPallet.id == pallet.id,
                    InventoryPallet.version == pallet.version,
                )
                .values(version=pallet.version + 1, updated_by=operator_id)
            )
            if pallet_result.rowcount != 1:
                raise WarehouseInventoryError(
                    "真实栈板已被其他操作更新，请刷新后重试", 409
                )

    db.flush()
    if pallet_item is None and target_location.source_version == "V11":
        from app.services.floor3_locations import (
            Floor3LocationError,
            bind_finished_lot_to_floor3_pallet,
        )

        try:
            bind_finished_lot_to_floor3_pallet(
                db,
                lot=lot,
                operator_id=operator_id,
            )
        except Floor3LocationError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error

    db.refresh(lot)
    audit = {
        "action": FINISHED_LOT_EDIT_REASON,
        "request": request_payload,
        "changed_fields": list(changes),
        "changes": changes,
    }
    _movement(
        db,
        lot=lot,
        movement_type="adjust",
        quantity=quantity_available - before["available"],
        before=before,
        operator_id=operator_id,
        reason=FINISHED_LOT_EDIT_REASON,
        remarks=json.dumps(audit, ensure_ascii=False, sort_keys=True),
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
    values: dict[str, object] = {
        "version": lot.version + 1,
        "last_movement_at": utc_now_naive(),
    }
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

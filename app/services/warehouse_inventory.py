from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.product import Product
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    SemiFinishedInventoryDetail,
    WarehouseLocation,
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        stock_date=stock_date,
        last_movement_at=now,
        remarks=remarks,
        created_by=operator_id,
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
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        return existing
    if quantity <= 0 or board_length_mm <= 0 or board_width_mm <= 0:
        raise WarehouseInventoryError("数量和纸板长宽必须大于0")
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
    now = utc_now()
    lot = InventoryLot(
        lot_number=_number("SI"),
        inventory_type="semi_finished",
        warehouse_location_id=location_id,
        quantity_available=quantity,
        unit="sheets",
        status="active",
        source_type=source_type,
        stock_date=stock_date,
        last_movement_at=now,
        remarks=remarks,
        created_by=operator_id,
    )
    db.add(lot)
    db.flush()
    lot.semi_finished_detail = SemiFinishedInventoryDetail(
        supplier_name=(supplier_name or "").strip() or None,
        owner_customer_id=customer.id if customer else None,
        owner_customer_name_snapshot=customer.name if customer else None,
        material_code_snapshot=material_code.strip().upper(),
        layer_count=layer_count,
        flute_type=flute,
        board_length_mm=board_length_mm,
        board_width_mm=board_width_mm,
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

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.core.time_contract import beijing_now_naive, beijing_today, utc_now_naive
from app.models.customer import Customer
from app.models.product import Product
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryMovement,
    InventoryLocationMovement,
    InventoryPallet,
    InventoryPalletItem,
    Floor3LocationLayout,
    WarehouseLocation,
)


class Floor3LocationError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


FLOOR3_LAYOUT_AREA_CODES = frozenset(
    {
        "A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2", "CD1", "D1", "D2",
        "DE1", "E1", "E2", "E3", "E4", "F1", "F2", "F3", "F4", "F12", "F34",
    }
)


@dataclass(frozen=True)
class Floor3MoveResult:
    pallet: InventoryPallet
    movement: InventoryLocationMovement
    replayed: bool



def _trim(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _validate_layout_geometry(
    *,
    left_pct: Decimal,
    top_pct: Decimal,
    width_pct: Decimal,
    height_pct: Decimal,
) -> None:
    maximum = Decimal("100")
    if left_pct < 0 or left_pct > maximum:
        raise Floor3LocationError("布局 left_pct 必须在 0 到 100 之间")
    if top_pct < 0 or top_pct > maximum:
        raise Floor3LocationError("布局 top_pct 必须在 0 到 100 之间")
    if width_pct <= 0 or width_pct > maximum:
        raise Floor3LocationError("布局 width_pct 必须大于 0 且不超过 100")
    if height_pct <= 0 or height_pct > maximum:
        raise Floor3LocationError("布局 height_pct 必须大于 0 且不超过 100")
    if left_pct + width_pct > maximum:
        raise Floor3LocationError(
            "布局不能超出地图右边界：left_pct + width_pct 不能超过 100"
        )
    if top_pct + height_pct > maximum:
        raise Floor3LocationError(
            "布局不能超出地图下边界：top_pct + height_pct 不能超过 100"
        )


def _location(db: Session, location_id: int) -> WarehouseLocation:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise Floor3LocationError("货位不存在", status_code=404)
    if not row.is_active:
        raise Floor3LocationError("货位已停用，不能绑定或移入栈板", status_code=409)
    if row.warehouse_floor != 3 or row.source_version != "V11":
        raise Floor3LocationError(
            "当前操作只允许三楼 V11 Phase A 货位", status_code=409
        )
    return row


def _pallet(
    db: Session,
    pallet_id: int,
    *,
    refresh: bool = False,
) -> InventoryPallet:
    query = (
        select(InventoryPallet)
        .options(
            selectinload(InventoryPallet.items).selectinload(
                InventoryPalletItem.inventory_lot
            )
        )
        .where(InventoryPallet.id == pallet_id)
    )
    if refresh:
        query = query.execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise Floor3LocationError("栈板不存在", status_code=404)
    if row.location_id is not None:
        _location(db, row.location_id)
    return row


def _claim_pallet_version(
    db: Session,
    row: InventoryPallet,
    *,
    expected_version: int,
) -> None:
    """Atomically claim the next pallet version before any business mutation."""
    result = db.execute(
        update(InventoryPallet)
        .where(
            InventoryPallet.id == row.id,
            InventoryPallet.version == expected_version,
        )
        .values(version=expected_version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise Floor3LocationError(
            "栈板已被其他操作更新，请刷新后重试", status_code=409
        )
    set_committed_value(row, "version", expected_version + 1)


def _active_pallet_at(db: Session, location_id: int) -> InventoryPallet | None:
    return db.scalar(
        select(InventoryPallet).where(
            InventoryPallet.location_id == location_id,
            InventoryPallet.is_current.is_(True),
        )
    )


def _active_pallet_exists(location_id: int):
    return (
        select(InventoryPallet.id)
        .where(
            InventoryPallet.location_id == location_id,
            InventoryPallet.is_current.is_(True),
        )
        .exists()
    )


def _claim_empty_active_location(
    db: Session,
    location: WarehouseLocation,
) -> None:
    """Serialize occupancy with slot disabling using the SQLite writer lock."""
    result = db.execute(
        update(WarehouseLocation)
        .where(
            WarehouseLocation.id == location.id,
            WarehouseLocation.warehouse_floor == 3,
            WarehouseLocation.source_version == "V11",
            WarehouseLocation.is_active.is_(True),
            ~_active_pallet_exists(location.id),
        )
        .values(
            # This guarded no-op is the first write in an occupancy operation.
            # It acquires SQLite's single-writer lock without changing timestamps.
            is_active=WarehouseLocation.is_active,
            updated_at=WarehouseLocation.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        return
    is_active = db.scalar(
        select(WarehouseLocation.is_active).where(
            WarehouseLocation.id == location.id,
            WarehouseLocation.warehouse_floor == 3,
            WarehouseLocation.source_version == "V11",
        )
    )
    if is_active is not True:
        raise Floor3LocationError("货位已停用，不能绑定或移入栈板", status_code=409)
    if _active_pallet_at(db, location.id) is not None:
        raise Floor3LocationError("目标货位已有当前栈板", status_code=409)
    raise Floor3LocationError("货位状态已变化，请刷新后重试", status_code=409)


def _floor3_area_anchor(db: Session, area_code: str) -> WarehouseLocation:
    if area_code not in FLOOR3_LAYOUT_AREA_CODES:
        raise Floor3LocationError("三楼区域不存在", status_code=404)
    row = db.scalar(
        select(WarehouseLocation)
        .where(
            WarehouseLocation.warehouse_floor == 3,
            WarehouseLocation.source_version == "V11",
            WarehouseLocation.area_code == area_code,
        )
        .order_by(WarehouseLocation.id)
    )
    if row is None:
        raise Floor3LocationError("三楼区域不存在", status_code=404)
    return row


def create_layout_slot(
    db: Session,
    *,
    area_code: str,
    location_code: str,
    location_name: str,
    left_pct: Decimal,
    top_pct: Decimal,
    width_pct: Decimal,
    height_pct: Decimal,
    z_index: int,
    operator_id: int,
) -> WarehouseLocation:
    area = area_code.strip().upper()
    code = location_code.strip()
    _validate_layout_geometry(
        left_pct=left_pct,
        top_pct=top_pct,
        width_pct=width_pct,
        height_pct=height_pct,
    )
    if not code.startswith(f"{area}-"):
        raise Floor3LocationError("货位编码必须以区域编码加连字符开头")
    _floor3_area_anchor(db, area)
    if db.scalar(select(WarehouseLocation.id).where(WarehouseLocation.location_code == code)):
        raise Floor3LocationError("货位编码已存在", status_code=409)

    anchor = _floor3_area_anchor(db, area)
    sort_order = (
        db.scalar(
            select(func.max(WarehouseLocation.sort_order)).where(
                WarehouseLocation.warehouse_floor == 3,
                WarehouseLocation.source_version == "V11",
            )
        )
        or 0
    ) + 1
    location = WarehouseLocation(
        location_code=code,
        location_name=location_name.strip(),
        warehouse_type=anchor.warehouse_type,
        warehouse_floor=3,
        area_code=area,
        storage_type=anchor.storage_type,
        sort_order=sort_order,
        is_temporary=anchor.is_temporary,
        source_version="V11",
    )
    location.floor3_layout = Floor3LocationLayout(
        left_pct=left_pct,
        top_pct=top_pct,
        width_pct=width_pct,
        height_pct=height_pct,
        z_index=z_index,
        version=1,
        source_type="manual",
        created_by=operator_id,
        updated_by=operator_id,
    )
    db.add(location)
    db.flush()
    return location


def update_layout_area(
    db: Session,
    *,
    area_code: str,
    slots: list[dict],
    operator_id: int,
) -> list[Floor3LocationLayout]:
    area = area_code.strip().upper()
    _floor3_area_anchor(db, area)
    if len({slot["location_id"] for slot in slots}) != len(slots):
        raise Floor3LocationError("批量布局不能重复同一货位")
    for slot in slots:
        _validate_layout_geometry(
            left_pct=slot["left_pct"],
            top_pct=slot["top_pct"],
            width_pct=slot["width_pct"],
            height_pct=slot["height_pct"],
        )

    for slot in slots:
        layout = db.scalar(
            select(Floor3LocationLayout)
            .join(WarehouseLocation)
            .where(
                Floor3LocationLayout.location_id == slot["location_id"],
                WarehouseLocation.warehouse_floor == 3,
                WarehouseLocation.source_version == "V11",
                WarehouseLocation.area_code == area,
            )
        )
        if layout is None:
            raise Floor3LocationError("布局货位不存在或不属于该区域", status_code=404)
        result = db.execute(
            update(Floor3LocationLayout)
            .where(
                Floor3LocationLayout.id == layout.id,
                Floor3LocationLayout.version == slot["expected_version"],
            )
            .values(
                left_pct=slot["left_pct"],
                top_pct=slot["top_pct"],
                width_pct=slot["width_pct"],
                height_pct=slot["height_pct"],
                z_index=slot["z_index"],
                version=slot["expected_version"] + 1,
                updated_by=operator_id,
                updated_at=beijing_now_naive(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise Floor3LocationError("布局已被其他操作更新，请刷新后重试", status_code=409)
        db.expire(layout)
    db.flush()
    return db.scalars(
        select(Floor3LocationLayout)
        .where(Floor3LocationLayout.location_id.in_([slot["location_id"] for slot in slots]))
        .order_by(Floor3LocationLayout.location_id)
    ).all()


def set_layout_slot_active(
    db: Session,
    *,
    location_id: int,
    is_active: bool,
    expected_version: int,
    operator_id: int,
) -> WarehouseLocation:
    location_state_guard = select(WarehouseLocation.id).where(
        WarehouseLocation.id == location_id,
        WarehouseLocation.warehouse_floor == 3,
        WarehouseLocation.source_version == "V11",
        WarehouseLocation.is_active.is_(not is_active),
    )
    if not is_active:
        location_state_guard = location_state_guard.where(
            ~_active_pallet_exists(location_id)
        )

    layout_result = db.execute(
        update(Floor3LocationLayout)
        .where(
            Floor3LocationLayout.location_id == location_id,
            Floor3LocationLayout.version == expected_version,
            location_state_guard.exists(),
        )
        .values(
            version=expected_version + 1,
            updated_by=operator_id,
            updated_at=beijing_now_naive(),
        )
        .execution_options(synchronize_session=False)
    )
    if layout_result.rowcount != 1:
        location = db.scalar(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.floor3_layout))
            .where(
                WarehouseLocation.id == location_id,
                WarehouseLocation.warehouse_floor == 3,
                WarehouseLocation.source_version == "V11",
            )
        )
        if location is None or location.floor3_layout is None:
            raise Floor3LocationError("布局货位不存在", status_code=404)
        if not is_active and _active_pallet_at(db, location.id) is not None:
            raise Floor3LocationError("货位仍被栈板占用", status_code=409)
        if location.is_active == is_active:
            raise Floor3LocationError("货位已处于该状态", status_code=409)
        raise Floor3LocationError(
            "布局已被其他操作更新，请刷新后重试", status_code=409
        )

    location_conditions = [
        WarehouseLocation.id == location_id,
        WarehouseLocation.warehouse_floor == 3,
        WarehouseLocation.source_version == "V11",
        WarehouseLocation.is_active.is_(not is_active),
    ]
    if not is_active:
        location_conditions.append(~_active_pallet_exists(location_id))
    location_result = db.execute(
        update(WarehouseLocation)
        .where(*location_conditions)
        .values(is_active=is_active, updated_at=beijing_now_naive())
        .execution_options(synchronize_session=False)
    )
    if location_result.rowcount != 1:
        raise Floor3LocationError("货位状态已变化，请刷新后重试", status_code=409)

    location = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id == location_id)
        .execution_options(populate_existing=True)
    )
    assert location is not None and location.floor3_layout is not None
    return location


def _generated_pallet_code() -> str:
    return f"PLT-3F-{beijing_now_naive():%Y%m%d}-{uuid4().hex[:8].upper()}"


def _product_snapshot(
    db: Session,
    *,
    product_id: int,
    customer_id: int | None,
) -> tuple[Product, Customer]:
    product = db.get(Product, product_id)
    if product is None or not product.is_active or product.deleted_at is not None:
        raise Floor3LocationError("所选产品不存在或已停用", status_code=409)
    if customer_id is not None and product.customer_id != customer_id:
        raise Floor3LocationError("所选产品不属于当前客户", status_code=409)
    customer = db.get(Customer, product.customer_id)
    if customer is None:
        raise Floor3LocationError("所选产品缺少有效客户", status_code=409)
    return product, customer


def _build_item(
    db: Session,
    *,
    pallet_id: int,
    item: dict,
    operator_id: int | None,
) -> InventoryPalletItem:
    inventory_lot_id = item.get("inventory_lot_id")
    inventory_lot: InventoryLot | None = None
    if inventory_lot_id is not None:
        inventory_lot = db.scalar(
            select(InventoryLot)
            .options(selectinload(InventoryLot.finished_detail))
            .where(InventoryLot.id == int(inventory_lot_id))
        )
        if (
            inventory_lot is None
            or inventory_lot.inventory_type != "finished"
            or inventory_lot.finished_detail is None
        ):
            raise Floor3LocationError("正式成品库存批次不存在", status_code=409)
        if inventory_lot.pallet_item is not None:
            raise Floor3LocationError("该成品库存批次已经绑定三楼栈板", status_code=409)
        detail = inventory_lot.finished_detail
        product_id = detail.product_id
        customer_id = detail.owner_customer_id
        item = {
            **item,
            "product_id": product_id,
            "customer_id": customer_id,
            "inventory_code": detail.inventory_code_snapshot,
            "product_name": detail.product_name_snapshot,
            "item_type": "finished",
            "quantity": max(
                int(inventory_lot.quantity_available or 0)
                + int(inventory_lot.quantity_reserved or 0),
                1,
            ),
            "unit": inventory_lot.unit,
            "match_status": "matched",
        }
    product_id = item.get("product_id")
    customer_id = item.get("customer_id")
    match_status = str(item.get("match_status") or "matched").strip()
    if match_status not in {"matched", "pending"}:
        raise Floor3LocationError("产品匹配状态无效")

    inventory_code = _trim(item.get("inventory_code"))
    product_name = _trim(item.get("product_name"))
    order_no = _trim(item.get("order_no"))
    customer_name_snapshot = None
    if product_id is not None:
        product, customer = _product_snapshot(
            db,
            product_id=int(product_id),
            customer_id=int(customer_id) if customer_id is not None else None,
        )
        customer_id = customer.id
        inventory_code = product.product_code or product.customer_material_code
        product_name = product.product_name
        customer_name_snapshot = customer.name
        match_status = "matched"
    else:
        if match_status != "pending":
            raise Floor3LocationError("未选择产品时只能暂存为待匹配")
        if customer_id is not None:
            customer = db.get(Customer, int(customer_id))
            if customer is None:
                raise Floor3LocationError("客户不存在", status_code=409)
            customer_name_snapshot = customer.name
        if not any((inventory_code, order_no, product_name)):
            raise Floor3LocationError("待匹配内容至少填写存货编码、订单号或产品名称之一")

    item_type = str(item.get("item_type") or "finished").strip()
    if item_type not in {"finished", "semi_finished", "raw_material"}:
        raise Floor3LocationError("货物类型无效")
    try:
        quantity = Decimal(str(item.get("quantity")))
    except Exception as error:  # pragma: no cover - Pydantic handles normal API input
        raise Floor3LocationError("数量格式无效") from error
    if quantity <= 0:
        raise Floor3LocationError("数量必须大于0")
    unit = _trim(item.get("unit")) or ("boxes" if item_type == "finished" else "sheets")

    return InventoryPalletItem(
        pallet_id=pallet_id,
        inventory_lot=inventory_lot,
        customer_id=int(customer_id) if customer_id is not None else None,
        product_id=int(product_id) if product_id is not None else None,
        inventory_code=inventory_code,
        order_no=order_no,
        customer_name_snapshot=customer_name_snapshot,
        product_name=product_name,
        item_type=item_type,
        quantity=quantity,
        unit=unit,
        match_status=match_status,
        remarks=_trim(item.get("remarks")),
        created_by=operator_id,
    )


def bind_finished_lot_to_floor3_pallet(
    db: Session,
    *,
    lot: InventoryLot,
    operator_id: int | None,
    pallet_id: int | None = None,
    pallet_code: str | None = None,
    require_empty_pallet: bool = False,
) -> InventoryPallet:
    """Bind one official finished-goods lot to its physical floor-three slot.

    The inventory lot remains the only quantity ledger.  The pallet item is a
    location projection and its API quantity is read from the linked lot.
    """
    if lot.inventory_type != "finished" or lot.finished_detail is None:
        raise Floor3LocationError("只有正式成品库存可以绑定三楼货位")
    location = _location(db, lot.warehouse_location_id)
    if lot.pallet_item is not None:
        return _pallet(db, lot.pallet_item.pallet_id)
    if pallet_id is not None:
        pallet = _pallet(db, pallet_id)
        if not pallet.is_current or pallet.location_id != location.id:
            raise Floor3LocationError("指定栈板不在该三楼货位", status_code=409)
        return add_pallet_item(
            db,
            pallet_id=pallet.id,
            expected_version=pallet.version,
            item={
                "inventory_lot_id": lot.id,
                "item_type": "finished",
                "quantity": max(
                    int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0),
                    1,
                ),
                "unit": lot.unit,
                "match_status": "matched",
            },
            operator_id=operator_id,
        )
    item = {
        "inventory_lot_id": lot.id,
        "item_type": "finished",
        "quantity": max(
            int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0), 1
        ),
        "unit": lot.unit,
        "match_status": "matched",
    }
    pallet = _active_pallet_at(db, location.id)
    if require_empty_pallet and pallet is not None:
        raise Floor3LocationError(
            "目标货位已有当前栈板，正式成品创建必须选择空闲货位", status_code=409
        )
    if pallet is None:
        return create_pallet(
            db,
            location_id=location.id,
            pallet_code=pallet_code,
            items=[item],
            remarks="成品入库自动绑定",
            operator_id=operator_id,
        )
    return add_pallet_item(
        db,
        pallet_id=pallet.id,
        expected_version=pallet.version,
        item=item,
        operator_id=operator_id,
    )


def _linked_inventory_lots(db: Session, pallet_id: int) -> list[InventoryLot]:
    return list(
        db.scalars(
            select(InventoryLot)
            .join(
                InventoryPalletItem,
                InventoryPalletItem.inventory_lot_id == InventoryLot.id,
            )
            .where(InventoryPalletItem.pallet_id == pallet_id)
            .with_for_update()
        ).all()
    )


def _needs_relocation(location: WarehouseLocation, items: list[InventoryPalletItem]) -> bool:
    if location.is_temporary:
        return True
    expected = location.warehouse_type
    if expected == "shared":
        return False
    for item in items:
        if expected == "finished" and item.item_type != "finished":
            return True
        if expected == "semi_finished" and item.item_type == "finished":
            return True
    return False


def create_pallet(
    db: Session,
    *,
    location_id: int,
    pallet_code: str | None,
    items: list[dict],
    remarks: str | None,
    operator_id: int | None,
) -> InventoryPallet:
    official_items = [
        item for item in items if item.get("create_finished_inventory") is True
    ]
    if official_items:
        return _create_pallet_with_official_items(
            db,
            location_id=location_id,
            pallet_code=pallet_code,
            official_items=official_items,
            snapshot_items=[
                item for item in items if item.get("create_finished_inventory") is not True
            ],
            remarks=remarks,
            operator_id=operator_id,
        )
    location = _location(db, location_id)
    try:
        _claim_empty_active_location(db, location)
    except Floor3LocationError as error:
        if str(error) == "目标货位已有当前栈板":
            raise Floor3LocationError(
                "该货位已有当前栈板，请先移位或清空", status_code=409
            ) from error
        raise
    if not items:
        raise Floor3LocationError("栈板至少需要一条内容")

    row = InventoryPallet(
        pallet_code=_trim(pallet_code) or _generated_pallet_code(),
        location_id=location.id,
        status="active",
        is_current=True,
        needs_relocation=False,
        remarks=_trim(remarks),
        created_by=operator_id,
        updated_by=operator_id,
    )
    db.add(row)
    db.flush()
    row.items.extend(
        _build_item(db, pallet_id=row.id, item=item, operator_id=operator_id)
        for item in items
    )
    row.needs_relocation = _needs_relocation(location, row.items)
    db.add(
        InventoryLocationMovement(
            pallet_id=row.id,
            from_location_id=None,
            to_location_id=location.id,
            movement_type="create",
            operator_id=operator_id,
            moved_at=beijing_now_naive(),
            remarks=_trim(remarks),
        )
    )
    db.flush()
    return row


def _create_pallet_with_official_items(
    db: Session,
    *,
    location_id: int,
    pallet_code: str | None,
    official_items: list[dict],
    snapshot_items: list[dict],
    remarks: str | None,
    operator_id: int | None,
) -> InventoryPallet:
    from app.services.warehouse_inventory import manual_finished_in

    pallet_id: int | None = None
    for index, item in enumerate(official_items):
        if (
            item.get("item_type") != "finished"
            or item.get("match_status") != "matched"
            or item.get("customer_id") is None
            or item.get("product_id") is None
            or not item.get("idempotency_key")
        ):
            raise Floor3LocationError(
                "正式成品入库必须提供客户、产品、匹配状态和幂等键", status_code=422
            )
        quantity = Decimal(str(item.get("quantity")))
        if quantity != quantity.to_integral_value():
            raise Floor3LocationError("正式成品入库数量必须是正整数", status_code=422)
        existing_movement = db.scalar(
            select(InventoryMovement).where(
                InventoryMovement.idempotency_key == item["idempotency_key"],
                InventoryMovement.movement_type == "manual_in",
            )
        )
        if existing_movement is not None:
            existing_lot = db.get(InventoryLot, existing_movement.inventory_lot_id)
            existing_item = (
                existing_lot.pallet_item
                if existing_lot is not None
                else None
            )
            if (
                existing_item is None
                or existing_item.pallet is None
                or existing_item.pallet.location_id != location_id
            ):
                raise Floor3LocationError(
                    "幂等键已用于其它物理栈板，不能重复创建", status_code=409
                )
            pallet_id = existing_item.pallet_id
        lot = manual_finished_in(
            db,
            customer_id=int(item["customer_id"]),
            product_id=int(item["product_id"]),
            location_id=location_id,
            quantity=int(quantity),
            stock_date=item.get("stock_date") or beijing_today(),
            source_type="manual",
            remarks=item.get("remarks") or remarks,
            operator_id=operator_id,
            idempotency_key=item["idempotency_key"],
            pallet_id=pallet_id,
            pallet_code=pallet_code if index == 0 else None,
            require_empty_pallet=pallet_id is None,
        )
        pallet_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == lot.id
            )
        )
        if pallet_item is None:
            raise Floor3LocationError("正式成品批次未能绑定当前物理栈板", status_code=409)
        pallet_id = pallet_item.pallet_id

    pallet = _pallet(db, pallet_id) if pallet_id is not None else None
    if pallet is None:
        raise Floor3LocationError("正式成品栈板创建失败", status_code=409)
    for item in snapshot_items:
        pallet = add_pallet_item(
            db,
            pallet_id=pallet.id,
            expected_version=pallet.version,
            item=item,
            operator_id=operator_id,
        )
    return _pallet(db, pallet.id, refresh=True)


def add_pallet_item(
    db: Session,
    *,
    pallet_id: int,
    expected_version: int,
    item: dict,
    operator_id: int | None,
) -> InventoryPallet:
    row = _pallet(db, pallet_id)
    if not row.is_current or row.location_id is None:
        raise Floor3LocationError("栈板已清空或移出，不能继续增加内容", status_code=409)
    location = _location(db, row.location_id)
    _claim_pallet_version(db, row, expected_version=expected_version)
    row.items.append(
        _build_item(db, pallet_id=row.id, item=item, operator_id=operator_id)
    )
    row.needs_relocation = _needs_relocation(location, row.items)
    row.updated_by = operator_id
    db.add(
        InventoryLocationMovement(
            pallet_id=row.id,
            from_location_id=location.id,
            to_location_id=location.id,
            movement_type="add_item",
            operator_id=operator_id,
            moved_at=beijing_now_naive(),
            remarks=f"增加同栈板内容：{_trim(item.get('inventory_code')) or _trim(item.get('product_name')) or '待匹配'}",
        )
    )
    db.flush()
    return row


def convert_snapshot_to_finished_lot(
    db: Session,
    *,
    pallet_id: int,
    item_id: int,
    expected_version: int,
    idempotency_key: str,
    stock_date: date,
    operator_id: int | None,
) -> tuple[InventoryPallet, InventoryLot, bool]:
    """Promote one matched snapshot to one official finished-goods lot."""
    existing_movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key,
            InventoryMovement.movement_type == "manual_in",
        )
    )
    if existing_movement is not None:
        lot = db.get(InventoryLot, existing_movement.inventory_lot_id)
        if lot is None or lot.pallet_item is None or lot.pallet_item.pallet_id != pallet_id:
            raise Floor3LocationError("幂等键已用于不同的正式成品入库", status_code=409)
        return _pallet(db, pallet_id, refresh=True), lot, True

    pallet = _pallet(db, pallet_id)
    if not pallet.is_current or pallet.location_id is None:
        raise Floor3LocationError("栈板当前不在有效三楼货位", status_code=409)
    if pallet.version != expected_version:
        raise Floor3LocationError("栈板已被其他操作更新，请刷新后重试", status_code=409)
    item = next((row for row in pallet.items if row.id == item_id), None)
    if item is None:
        raise Floor3LocationError("栈板内容不存在", status_code=404)
    if item.inventory_lot_id is not None:
        raise Floor3LocationError("该内容已经是正式成品库存", status_code=409)
    if (
        item.item_type not in {"finished", "semi_finished"}
        or item.match_status != "matched"
        or item.customer_id is None
        or item.product_id is None
    ):
        raise Floor3LocationError(
            "只能将客户、产品完整且已匹配的成品或半成品快照转为正式成品库存",
            status_code=422,
        )
    quantity = Decimal(str(item.quantity))
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise Floor3LocationError("正式成品入库数量必须是正整数", status_code=422)

    from app.services.warehouse_inventory import manual_finished_in

    lot = manual_finished_in(
        db,
        customer_id=item.customer_id,
        product_id=item.product_id,
        location_id=pallet.location_id,
        quantity=int(quantity),
        stock_date=stock_date,
        source_type="manual",
        remarks=(
            "现场快照转正式成品库存；"
            f"原快照类型：{item.item_type}；原单位：{item.unit}"
        ),
        operator_id=operator_id,
        idempotency_key=idempotency_key,
        pallet_id=pallet.id,
    )
    official_item = db.scalar(
        select(InventoryPalletItem).where(
            InventoryPalletItem.inventory_lot_id == lot.id,
            InventoryPalletItem.pallet_id == pallet.id,
        )
    )
    if official_item is None:
        raise Floor3LocationError("正式成品批次未能绑定当前物理栈板", status_code=409)
    db.delete(item)
    db.flush()
    return _pallet(db, pallet.id, refresh=True), lot, False


def _idempotent_move_result(
    db: Session,
    movement: InventoryLocationMovement,
    *,
    pallet_id: int,
    expected_version: int,
    to_location_id: int,
    remarks: str | None,
) -> Floor3MoveResult:
    if (
        movement.movement_type != "move"
        or movement.pallet_id != pallet_id
        or movement.to_location_id != to_location_id
        or movement.pallet_version_before != expected_version
        or _trim(movement.remarks) != _trim(remarks)
    ):
        raise Floor3LocationError("幂等键已用于不同的移位业务", status_code=409)
    return Floor3MoveResult(
        pallet=_pallet(db, pallet_id, refresh=True), movement=movement, replayed=True
    )


def _movement_by_idempotency_key(
    db: Session,
    idempotency_key: str,
) -> InventoryLocationMovement | None:
    return db.scalar(
        select(InventoryLocationMovement).where(
            InventoryLocationMovement.idempotency_key == idempotency_key
        )
    )


def move_pallet(
    db: Session,
    *,
    pallet_id: int,
    expected_version: int,
    to_location_id: int,
    remarks: str | None,
    operator_id: int | None,
    idempotency_key: str,
) -> Floor3MoveResult:
    existing = _movement_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return _idempotent_move_result(
            db,
            existing,
            pallet_id=pallet_id,
            expected_version=expected_version,
            to_location_id=to_location_id,
            remarks=remarks,
        )

    try:
        row = _pallet(db, pallet_id)
        if not row.is_current or row.location_id is None:
            raise Floor3LocationError(
                "栈板当前不在有效货位，不能移位", status_code=409
            )
        if row.location_id == to_location_id:
            raise Floor3LocationError("目标货位与当前货位相同")
        source = _location(db, row.location_id)
        if source.storage_type == "rack":
            raise Floor3LocationError("真实木栈板不能从货架格移出", status_code=409)
        target = _location(db, to_location_id)
        if target.storage_type == "rack":
            raise Floor3LocationError("真实木栈板不能移入货架格", status_code=409)
        # Acquire SQLite's writer lock before opening a savepoint. Two deferred
        # read transactions cannot reliably upgrade to writers concurrently.
        _claim_empty_active_location(db, target)

        # A duplicate may have committed while this request waited for that
        # writer lock. Recheck before claiming the pallet version.
        existing = _movement_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return _idempotent_move_result(
                db,
                existing,
                pallet_id=pallet_id,
                expected_version=expected_version,
                to_location_id=to_location_id,
                remarks=remarks,
            )

        with db.begin_nested():
            version_before = row.version
            _claim_pallet_version(db, row, expected_version=expected_version)
            from_location_id = row.location_id
            row.location_id = target.id
            row.status = "active"
            row.needs_relocation = _needs_relocation(target, row.items)
            row.updated_by = operator_id
            for lot in _linked_inventory_lots(db, row.id):
                lot.warehouse_location_id = target.id
                lot.version += 1
                lot.last_movement_at = utc_now_naive()
            movement = InventoryLocationMovement(
                pallet_id=row.id,
                from_location_id=from_location_id,
                to_location_id=target.id,
                movement_type="move",
                operator_id=operator_id,
                moved_at=beijing_now_naive(),
                idempotency_key=idempotency_key,
                confirmed_at=beijing_now_naive(),
                pallet_version_before=version_before,
                pallet_version_after=row.version,
                remarks=_trim(remarks),
            )
            db.add(movement)
            db.flush()
    except (Floor3LocationError, IntegrityError) as error:
        existing = _movement_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return _idempotent_move_result(
                db,
                existing,
                pallet_id=pallet_id,
                expected_version=expected_version,
                to_location_id=to_location_id,
                remarks=remarks,
            )
        raise
    return Floor3MoveResult(pallet=row, movement=movement, replayed=False)


def clear_pallet(
    db: Session,
    *,
    pallet_id: int,
    expected_version: int,
    remarks: str | None,
    operator_id: int | None,
) -> InventoryPallet:
    row = _pallet(db, pallet_id)
    if not row.is_current or row.location_id is None:
        raise Floor3LocationError("栈板已经清空", status_code=409)
    linked_lots = _linked_inventory_lots(db, row.id)
    if any(
        int(lot.quantity_available or 0)
        + int(lot.quantity_reserved or 0)
        + int(lot.quantity_damaged or 0)
        > 0
        for lot in linked_lots
    ):
        raise Floor3LocationError(
            "该栈板仍有关联的正式成品库存，请先完成出库或库存调整",
            status_code=409,
        )
    _claim_pallet_version(db, row, expected_version=expected_version)
    from_location_id = row.location_id
    row.location_id = None
    row.status = "closed"
    row.is_current = False
    row.needs_relocation = False
    row.closed_at = beijing_now_naive()
    row.updated_by = operator_id
    db.add(
        InventoryLocationMovement(
            pallet_id=row.id,
            from_location_id=from_location_id,
            to_location_id=None,
            movement_type="clear",
            operator_id=operator_id,
            moved_at=beijing_now_naive(),
            remarks=_trim(remarks),
        )
    )
    db.flush()
    return row


def set_pallet_relocation(
    db: Session,
    *,
    pallet_id: int,
    expected_version: int,
    needs_relocation: bool,
    operator_id: int | None,
) -> InventoryPallet:
    row = _pallet(db, pallet_id)
    if not row.is_current or row.location_id is None:
        raise Floor3LocationError("栈板已清空或移出，不能修改归位标记", status_code=409)
    location = _location(db, row.location_id)
    if not needs_relocation and _needs_relocation(location, row.items):
        raise Floor3LocationError(
            "当前为过道临放或货物类型与货位不一致，不能取消待归位",
            status_code=409,
        )
    _claim_pallet_version(db, row, expected_version=expected_version)
    row.needs_relocation = needs_relocation
    row.updated_by = operator_id
    db.flush()
    return row

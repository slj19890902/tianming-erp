from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import math
import re
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
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
from app.services.location_candidates import (
    claim_active_placed_location,
    operational_location_issue,
)


class Floor3LocationError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


FLOOR3_LAYOUT_AREA_CODES = frozenset(
    {
        "A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2", "CD1", "D1", "D2",
        "DE1", "E1", "E2", "E3", "E4", "SEMI-011", "F1", "F2", "F3", "F4", "F12", "F34",
    }
)


@dataclass(frozen=True)
class Floor3MoveResult:
    pallet: InventoryPallet
    movement: InventoryLocationMovement
    replayed: bool


@dataclass(frozen=True)
class Floor3MergeResult:
    source_pallet: InventoryPallet
    target_pallet: InventoryPallet
    source_movement: InventoryLocationMovement
    target_movement: InventoryLocationMovement
    moved_item_count: int
    replayed: bool


@dataclass(frozen=True)
class PalletMergeProfile:
    customer_id: int
    inventory_type: str
    unit: str
    lot_status: str


@dataclass(frozen=True)
class Floor3AreaLocationCountResult:
    area_code: str
    target_count: int
    active_count: int
    created: tuple[WarehouseLocation, ...]
    enabled: tuple[WarehouseLocation, ...]
    disabled: tuple[WarehouseLocation, ...]



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
    if row.warehouse_floor != 3 or row.source_version not in {"V11", "CURRENT_MAP"}:
        raise Floor3LocationError(
            "当前操作只允许已启用的三楼实测货位", status_code=409
        )
    if row.source_version == "CURRENT_MAP":
        current_pallet_id = db.scalar(
            select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id == row.id,
                InventoryPallet.is_current.is_(True),
            )
            .limit(1)
        )
        issue = operational_location_issue(
            db,
            row,
            pallet_storage_only=current_pallet_id is None,
            require_published=True,
            require_map_geometry=True,
        )
        if issue:
            raise Floor3LocationError(f"{issue}，不能办理当前栈板操作", status_code=409)
    return row


def _operational_pallet_location(
    db: Session,
    location_id: int,
    *,
    require_published: bool = False,
    required_inventory_type: str | None = None,
    pallet_storage_only: bool = True,
) -> WarehouseLocation:
    """Resolve any published finished-goods ground slot used by the map UI."""

    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise Floor3LocationError("货位不存在", status_code=404)
    is_direct_dispatch = bool(
        row.location_code == "F1-DISPATCH-01"
        and row.source_version == "P1-25C"
        and row.warehouse_floor == 1
        and str(row.area_code or "").strip().upper() == "DISPATCH"
        and row.warehouse_type in {"finished", "shared"}
        and row.storage_type == "temporary_aisle"
        and row.is_active
        and (row.placement_status or "placed") == "placed"
        and required_inventory_type in {None, "finished"}
    )
    if require_published and is_direct_dispatch:
        dispatch_issue = operational_location_issue(
            db,
            row,
            warehouse_types={"finished", "shared"},
            pallet_storage_only=True,
        )
        if dispatch_issue:
            raise Floor3LocationError(
                f"一楼待送共享位置不可用：{dispatch_issue}",
                status_code=409,
            )
        return row
    issue = operational_location_issue(
        db,
        row,
        warehouse_types={required_inventory_type or "finished", "shared"},
        pallet_storage_only=pallet_storage_only,
        require_published=require_published,
        require_map_geometry=require_published,
        required_inventory_type=required_inventory_type,
        # A merge does not add a pallet to the area; it always removes at least
        # one source pallet.  Capacity therefore must not reject an otherwise
        # valid target simply because the area is already at its reviewed cap.
        capacity_source_location_id=row.id,
    )
    if issue:
        raise Floor3LocationError(f"目标货位不可用：{issue}", status_code=409)
    return row


def _pallet(
    db: Session,
    pallet_id: int,
    *,
    refresh: bool = False,
    allow_non_operational_source: bool = False,
) -> InventoryPallet:
    query = (
        select(InventoryPallet)
        .options(
            selectinload(InventoryPallet.items).selectinload(
                InventoryPalletItem.inventory_lot
            ).selectinload(InventoryLot.finished_detail),
            selectinload(InventoryPallet.items).selectinload(
                InventoryPalletItem.product
            ),
        )
        .where(InventoryPallet.id == pallet_id)
    )
    if refresh:
        query = query.execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise Floor3LocationError("栈板不存在", status_code=404)
    if row.location_id is not None and not allow_non_operational_source:
        location = db.get(WarehouseLocation, row.location_id)
        if location is None:
            raise Floor3LocationError("栈板所在库位不存在", status_code=409)
        issue = operational_location_issue(
            db,
            location,
            warehouse_types={"finished", "shared"},
        )
        if issue:
            raise Floor3LocationError(
                f"栈板所在库位不可用：{issue}",
                status_code=409,
            )
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


def _active_inventory_exists(
    location_id: int,
    *,
    excluded_lot_id: int | None = None,
):
    query = select(InventoryLot.id).where(
        InventoryLot.warehouse_location_id == location_id,
        InventoryLot.status.in_(("active", "frozen")),
        (
            InventoryLot.quantity_available
            + InventoryLot.quantity_reserved
            + InventoryLot.quantity_damaged
        )
        > 0,
    )
    if excluded_lot_id is not None:
        query = query.where(InventoryLot.id != excluded_lot_id)
    return query.exists()


def _claim_empty_active_location(
    db: Session,
    location: WarehouseLocation,
    *,
    allowed_inventory_lot_id: int | None = None,
    require_no_live_inventory: bool = False,
    expected_layout_version: int | None = None,
) -> None:
    """Serialize occupancy with slot disabling using the SQLite writer lock."""
    try:
        floor_claimed = claim_active_placed_location(
            db,
            int(location.id),
            expected_layout_version=expected_layout_version,
        )
    except OperationalError as error:
        raise Floor3LocationError(
            "目标货位正在被其他入库、移位或布局操作使用，请稍后重试",
            status_code=409,
        ) from error
    if not floor_claimed:
        raise Floor3LocationError(
            "目标货位或其当前发布地图版本已经变化，请刷新后重试",
            status_code=409,
        )

    claim_conditions = [
        WarehouseLocation.id == location.id,
        WarehouseLocation.is_active.is_(True),
        or_(
            WarehouseLocation.placement_status == "placed",
            WarehouseLocation.placement_status.is_(None),
        ),
        WarehouseLocation.warehouse_type.in_(("finished", "shared")),
        ~_active_pallet_exists(location.id),
    ]
    if require_no_live_inventory:
        claim_conditions.append(
            ~_active_inventory_exists(
                location.id,
                excluded_lot_id=allowed_inventory_lot_id,
            )
        )
    layout_exists = (
        select(Floor3LocationLayout.id)
        .where(Floor3LocationLayout.location_id == WarehouseLocation.id)
        .exists()
    )
    if expected_layout_version is not None:
        claim_conditions.append(
            select(Floor3LocationLayout.id)
            .where(
                Floor3LocationLayout.location_id == WarehouseLocation.id,
                Floor3LocationLayout.version == expected_layout_version,
            )
            .exists()
        )
    else:
        claim_conditions.append(
            or_(
                ~layout_exists,
                _active_inventory_exists(location.id),
            )
        )
    try:
        result = db.execute(
            update(WarehouseLocation)
            .where(*claim_conditions)
            .values(
                # This guarded no-op is the first write in an occupancy operation.
                # It acquires SQLite's single-writer lock without changing timestamps.
                is_active=WarehouseLocation.is_active,
                updated_at=WarehouseLocation.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
    except OperationalError as error:
        raise Floor3LocationError(
            "目标货位正在被其他入库、移位或布局操作使用，请稍后重试",
            status_code=409,
        ) from error
    if result.rowcount == 1:
        return
    is_active = db.scalar(
        select(WarehouseLocation.is_active).where(
            WarehouseLocation.id == location.id,
        )
    )
    if is_active is not True:
        raise Floor3LocationError("货位已停用，不能绑定或移入栈板", status_code=409)
    if _active_pallet_at(db, location.id) is not None:
        raise Floor3LocationError("目标货位已有当前栈板", status_code=409)
    if require_no_live_inventory and bool(
        db.scalar(
            select(
                _active_inventory_exists(
                    location.id,
                    excluded_lot_id=allowed_inventory_lot_id,
                )
            )
        )
    ):
        raise Floor3LocationError("目标货位已有活动库存", status_code=409)
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
    placement_status: str = "placed",
    layout_kind: str = "unknown",
    source_type: str = "manual",
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
    if placement_status not in {"placed", "unplaced"}:
        raise Floor3LocationError("库位布局状态无效")
    if layout_kind not in {
        "unknown",
        "physical_pallet",
        "physical_rack",
        "logical_anchor",
    }:
        raise Floor3LocationError("货位点位类型无效")
    if source_type not in {"manual", "seeded"}:
        raise Floor3LocationError("货位点位来源无效")
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
        placement_status=placement_status,
    )
    location.floor3_layout = Floor3LocationLayout(
        left_pct=left_pct,
        top_pct=top_pct,
        width_pct=width_pct,
        height_pct=height_pct,
        z_index=z_index,
        # A freshly hand-created point is already an explicit fixed fact.  Use
        # version 2 so it can never be confused with the historical system
        # layouts that older releases mistakenly stored as manual/version 1.
        version=2 if source_type == "manual" else 1,
        source_type=source_type,
        layout_kind=layout_kind,
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
                source_type="manual",
                version=slot["expected_version"] + 1,
                updated_by=operator_id,
                updated_at=beijing_now_naive(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise Floor3LocationError("布局已被其他操作更新，请刷新后重试", status_code=409)
        db.execute(
            update(WarehouseLocation)
            .where(
                WarehouseLocation.id == slot["location_id"],
                WarehouseLocation.warehouse_floor == 3,
                WarehouseLocation.source_version == "V11",
            )
            .values(placement_status="placed", updated_at=beijing_now_naive())
            .execution_options(synchronize_session=False)
        )
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
            ~_active_pallet_exists(location_id),
            ~_active_inventory_exists(location_id),
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
        if not is_active and db.scalar(
            select(InventoryLot.id).where(
                InventoryLot.warehouse_location_id == location.id,
                InventoryLot.status.in_(("active", "frozen")),
            ).limit(1)
        ) is not None:
            raise Floor3LocationError("货位仍有库存或预占，不能减少库位", status_code=409)
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
        location_conditions.extend(
            (~_active_pallet_exists(location_id), ~_active_inventory_exists(location_id))
        )
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


def _layout_rectangles(rows: list[WarehouseLocation]) -> list[tuple[Decimal, Decimal, Decimal, Decimal]]:
    rectangles: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for row in rows:
        layout = row.floor3_layout
        if layout is None:
            continue
        rectangles.append(
            (
                Decimal(layout.left_pct),
                Decimal(layout.top_pct),
                Decimal(layout.width_pct),
                Decimal(layout.height_pct),
            )
        )
    return rectangles


def _rectangles_overlap(
    left: tuple[Decimal, Decimal, Decimal, Decimal],
    right: tuple[Decimal, Decimal, Decimal, Decimal],
) -> bool:
    left_x, left_y, left_w, left_h = left
    right_x, right_y, right_w, right_h = right
    return (
        left_x < right_x + right_w
        and left_x + left_w > right_x
        and left_y < right_y + right_h
        and left_y + left_h > right_y
    )


def _suggested_layout_rectangles(
    *,
    target_count: int,
    needed: int,
    occupied: list[tuple[Decimal, Decimal, Decimal, Decimal]],
) -> list[tuple[Decimal, Decimal, Decimal, Decimal]]:
    if needed <= 0:
        return []
    density = max(target_count, needed, 1)
    columns = max(1, math.ceil(math.sqrt(density * 1.25)))
    rows = max(1, math.ceil(density / columns))
    width = Decimal(str(round(min(14.0, 92.0 / columns), 4)))
    height = Decimal(str(round(min(14.0, 92.0 / rows), 4)))
    candidates: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for multiplier in (1, 2, 3):
        candidate_columns = columns * multiplier
        candidate_rows = rows * multiplier
        cell_width = Decimal("96") / Decimal(candidate_columns)
        cell_height = Decimal("96") / Decimal(candidate_rows)
        candidate_width = min(width, cell_width * Decimal("0.82"))
        candidate_height = min(height, cell_height * Decimal("0.82"))
        for row_index in range(candidate_rows):
            for column_index in range(candidate_columns):
                left = Decimal("2") + cell_width * Decimal(column_index) + (cell_width - candidate_width) / 2
                top = Decimal("2") + cell_height * Decimal(row_index) + (cell_height - candidate_height) / 2
                candidate = (
                    left.quantize(Decimal("0.0001")),
                    top.quantize(Decimal("0.0001")),
                    candidate_width.quantize(Decimal("0.0001")),
                    candidate_height.quantize(Decimal("0.0001")),
                )
                if any(_rectangles_overlap(candidate, existing) for existing in [*occupied, *candidates]):
                    continue
                candidates.append(candidate)
                if len(candidates) == needed:
                    return candidates
    raise Floor3LocationError("区域剩余布局空间不足，请先调整现有库位位置后再增加", status_code=409)


def _location_serial(area_code: str, location_code: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(area_code)}-L(\d+)", location_code, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def adjust_area_location_count(
    db: Session,
    *,
    area_code: str,
    target_count: int,
    operator_id: int,
) -> Floor3AreaLocationCountResult:
    area = area_code.strip().upper()
    if target_count < 0 or target_count > 500:
        raise Floor3LocationError("目标库位数必须在 0 到 500 之间")
    _floor3_area_anchor(db, area)
    all_rows = list(
        db.scalars(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.floor3_layout))
            .where(
                WarehouseLocation.warehouse_floor == 3,
                WarehouseLocation.source_version == "V11",
                WarehouseLocation.area_code == area,
            )
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.id)
        ).all()
    )
    active_rows = [row for row in all_rows if row.is_active]
    current_count = len(active_rows)
    created: list[WarehouseLocation] = []
    enabled: list[WarehouseLocation] = []
    disabled: list[WarehouseLocation] = []

    def auto_managed_layout(row: WarehouseLocation) -> bool:
        layout = row.floor3_layout
        if layout is None:
            return False
        # Count changes never infer provenance from manual/version 1.  Historical
        # candidates become seeded only after the separate auto-arrange consent.
        return layout.source_type == "seeded"

    if target_count == current_count:
        return Floor3AreaLocationCountResult(
            area_code=area,
            target_count=target_count,
            active_count=current_count,
            created=(),
            enabled=(),
            disabled=(),
        )

    if target_count > current_count:
        needed = target_count - current_count
        reusable = [
            row
            for row in all_rows
            if not row.is_active
            and row.floor3_layout is not None
            and auto_managed_layout(row)
            and db.scalar(
                select(InventoryPallet.id).where(
                    InventoryPallet.location_id == row.id,
                    InventoryPallet.is_current.is_(True),
                ).limit(1)
            ) is None
            and db.scalar(
                select(_active_inventory_exists(row.id))
            ) is not True
        ]
        for row in reusable[:needed]:
            enabled.append(
                set_layout_slot_active(
                    db,
                    location_id=row.id,
                    is_active=True,
                    expected_version=row.floor3_layout.version,
                    operator_id=operator_id,
                )
            )
        needed -= len(enabled)
        if needed > 0:
            occupied = _layout_rectangles([*active_rows, *enabled])
            layouts = _suggested_layout_rectangles(
                target_count=target_count,
                needed=needed,
                occupied=occupied,
            )
            existing_serials = {
                value
                for row in all_rows
                if (value := _location_serial(area, row.location_code)) is not None
            }
            next_serial = max(existing_serials, default=0) + 1
            for left, top, width, height in layouts:
                while next_serial in existing_serials:
                    next_serial += 1
                code = f"{area}-L{next_serial:03d}"
                row = create_layout_slot(
                    db,
                    area_code=area,
                    location_code=code,
                    location_name=f"{area} 区 {next_serial:03d} 号位",
                    left_pct=left,
                    top_pct=top,
                    width_pct=width,
                    height_pct=height,
                    z_index=0,
                    operator_id=operator_id,
                    placement_status="unplaced",
                    layout_kind="logical_anchor",
                    source_type="seeded",
                )
                created.append(row)
                existing_serials.add(next_serial)
                next_serial += 1
    elif target_count < current_count:
        needed = current_count - target_count
        removable = []
        for row in reversed(active_rows):
            has_pallet = db.scalar(
                select(InventoryPallet.id).where(
                    InventoryPallet.location_id == row.id,
                    InventoryPallet.is_current.is_(True),
                ).limit(1)
            ) is not None
            has_inventory = bool(
                db.scalar(select(_active_inventory_exists(row.id)))
            )
            if not has_pallet and not has_inventory and row.floor3_layout is not None:
                if auto_managed_layout(row):
                    removable.append(row)
        if len(removable) < needed:
            raise Floor3LocationError(
                f"只能减少 {len(removable)} 个空闲系统货位；占用或尚未明确接管的历史货位不会被移除，请先自动排布确认",
                status_code=409,
            )
        for row in removable[:needed]:
            assert row.floor3_layout is not None
            disabled.append(
                set_layout_slot_active(
                    db,
                    location_id=row.id,
                    is_active=False,
                    expected_version=row.floor3_layout.version,
                    operator_id=operator_id,
                )
            )

    db.flush()
    return Floor3AreaLocationCountResult(
        area_code=area,
        target_count=target_count,
        active_count=target_count,
        created=tuple(created),
        enabled=tuple(enabled),
        disabled=tuple(disabled),
    )


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
        inventory_code = product.product_code or product.customer_material_code
        product_name = product.product_name
        if (
            inventory_lot is not None
            and inventory_lot.finished_detail is not None
            and inventory_lot.finished_detail.is_general
        ):
            customer_id = None
            customer_name_snapshot = None
        else:
            customer_id = customer.id
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


def _pallet_is_released_and_empty(row: InventoryPallet) -> bool:
    if row.is_current or row.location_id is not None or row.status != "closed":
        return False
    for item in row.items:
        lot = item.inventory_lot
        if lot is None:
            if Decimal(item.quantity or 0) > 0:
                return False
            continue
        if (
            int(lot.quantity_available or 0)
            + int(lot.quantity_reserved or 0)
            + int(lot.quantity_damaged or 0)
        ) > 0:
            return False
    return True


def _released_empty_pallet(
    db: Session,
    *,
    pallet_code: str | None,
) -> InventoryPallet | None:
    query = select(InventoryPallet).options(
        selectinload(InventoryPallet.items).selectinload(
            InventoryPalletItem.inventory_lot
        )
    )
    if pallet_code:
        row = db.scalar(query.where(InventoryPallet.pallet_code == pallet_code))
        if row is None:
            return None
        if not _pallet_is_released_and_empty(row):
            raise Floor3LocationError(
                "该实体栈板编号仍在使用或仍有货物，不能重复入库",
                status_code=409,
            )
        return row
    rows = db.scalars(
        query.where(
            InventoryPallet.status == "closed",
            InventoryPallet.is_current.is_(False),
            InventoryPallet.location_id.is_(None),
        ).order_by(
            InventoryPallet.closed_at.asc(),
            InventoryPallet.id.asc(),
        )
    ).all()
    return next((row for row in rows if _pallet_is_released_and_empty(row)), None)


def _reuse_released_pallet(
    db: Session,
    *,
    row: InventoryPallet,
    location: WarehouseLocation,
    items: list[dict],
    remarks: str | None,
    operator_id: int | None,
) -> InventoryPallet:
    if not _pallet_is_released_and_empty(row):
        raise Floor3LocationError("空栈板状态已变化，请刷新后重试", status_code=409)
    version_before = int(row.version)
    changed = db.execute(
        update(InventoryPallet)
        .where(
            InventoryPallet.id == row.id,
            InventoryPallet.version == version_before,
            InventoryPallet.status == "closed",
            InventoryPallet.is_current.is_(False),
            InventoryPallet.location_id.is_(None),
        )
        .values(
            location_id=location.id,
            status="active",
            is_current=True,
            needs_relocation=False,
            location_occupancy_key="PRIMARY",
            version=InventoryPallet.version + 1,
            closed_at=None,
            remarks=_trim(remarks),
            updated_by=operator_id,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise Floor3LocationError(
            "空栈板已被其他入库使用，请刷新后重试",
            status_code=409,
        )

    # Old rows are the previous cycle's zero-balance projection. The pallet
    # identity and every location movement remain available for audit.
    db.execute(
        delete(InventoryPalletItem).where(InventoryPalletItem.pallet_id == row.id)
    )
    db.flush()
    db.expire(row)
    new_items = [
        _build_item(db, pallet_id=row.id, item=item, operator_id=operator_id)
        for item in items
    ]
    db.add_all(new_items)
    db.flush()
    row.needs_relocation = _needs_relocation(location, new_items)
    now = beijing_now_naive()
    db.add(
        InventoryLocationMovement(
            pallet_id=row.id,
            from_location_id=None,
            to_location_id=location.id,
            movement_type="move",
            operator_id=operator_id,
            moved_at=now,
            confirmed_at=now,
            pallet_version_before=version_before,
            pallet_version_after=version_before + 1,
            remarks=_trim(remarks) or "已释放空栈板自动复用",
        )
    )
    db.flush()
    return _pallet(db, row.id, refresh=True)


def bind_finished_lot_to_floor3_pallet(
    db: Session,
    *,
    lot: InventoryLot,
    operator_id: int | None,
    pallet_id: int | None = None,
    pallet_code: str | None = None,
    require_empty_pallet: bool = False,
    allow_operational_location: bool = False,
    require_no_live_inventory: bool = False,
) -> InventoryPallet:
    """Bind one official finished-goods lot to its physical floor-three slot.

    The inventory lot remains the only quantity ledger.  The pallet item is a
    location projection and its API quantity is read from the linked lot.
    """
    if lot.inventory_type != "finished" or lot.finished_detail is None:
        raise Floor3LocationError("只有正式成品库存可以绑定三楼货位")
    location = (
        _operational_pallet_location(
            db,
            lot.warehouse_location_id,
            required_inventory_type="finished",
            pallet_storage_only=False,
        )
        if allow_operational_location
        else _location(db, lot.warehouse_location_id)
    )
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
            allow_operational_location=allow_operational_location,
            pallet_storage_only=False,
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
            allow_operational_location=allow_operational_location,
            allowed_inventory_lot_id=int(lot.id),
            require_no_live_inventory=require_no_live_inventory,
            required_inventory_type="finished",
            pallet_storage_only=False,
        )
    return add_pallet_item(
        db,
        pallet_id=pallet.id,
        expected_version=pallet.version,
        item=item,
        operator_id=operator_id,
        allow_operational_location=allow_operational_location,
        pallet_storage_only=False,
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
    allow_operational_location: bool = False,
    require_published_location: bool = False,
    required_inventory_type: str | None = None,
    allowed_inventory_lot_id: int | None = None,
    require_no_live_inventory: bool = False,
    expected_layout_version: int | None = None,
    pallet_storage_only: bool = True,
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
            expected_layout_version=expected_layout_version,
        )
    location = (
        _operational_pallet_location(
            db,
            location_id,
            require_published=require_published_location,
            required_inventory_type=required_inventory_type,
            pallet_storage_only=pallet_storage_only,
        )
        if allow_operational_location
        else _location(db, location_id)
    )
    try:
        _claim_empty_active_location(
            db,
            location,
            allowed_inventory_lot_id=allowed_inventory_lot_id,
            require_no_live_inventory=require_no_live_inventory,
            expected_layout_version=expected_layout_version,
        )
    except Floor3LocationError as error:
        if str(error) == "目标货位已有当前栈板":
            raise Floor3LocationError(
                "该货位已有当前栈板，请先移位或清空", status_code=409
            ) from error
        raise
    if allow_operational_location:
        # The floor mutex may have waited for a map publish.  Re-evaluate the
        # target against that now-current published projection before writing
        # any pallet or inventory fact.
        location = _operational_pallet_location(
            db,
            location_id,
            require_published=require_published_location,
            required_inventory_type=required_inventory_type,
            pallet_storage_only=pallet_storage_only,
        )
    if not items:
        raise Floor3LocationError("栈板至少需要一条内容")

    normalized_pallet_code = _trim(pallet_code)
    reusable = _released_empty_pallet(
        db,
        pallet_code=normalized_pallet_code,
    )
    if reusable is not None:
        return _reuse_released_pallet(
            db,
            row=reusable,
            location=location,
            items=items,
            remarks=remarks,
            operator_id=operator_id,
        )

    row = InventoryPallet(
        pallet_code=normalized_pallet_code or _generated_pallet_code(),
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
    expected_layout_version: int | None,
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
            expected_layout_version=expected_layout_version,
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
    allow_operational_location: bool = False,
    pallet_storage_only: bool = True,
) -> InventoryPallet:
    row = _pallet(db, pallet_id)
    if not row.is_current or row.location_id is None:
        raise Floor3LocationError("栈板已清空或移出，不能继续增加内容", status_code=409)
    location = (
        _operational_pallet_location(
            db,
            row.location_id,
            pallet_storage_only=pallet_storage_only,
        )
        if allow_operational_location
        else _location(db, row.location_id)
    )
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


def _merge_target_idempotency_key(idempotency_key: str) -> str:
    return f"{idempotency_key}:target"


def _remaining_pallet_items(pallet: InventoryPallet) -> list[InventoryPalletItem]:
    remaining: list[InventoryPalletItem] = []
    for item in pallet.items:
        lot = item.inventory_lot
        quantity = (
            int(lot.quantity_available or 0)
            + int(lot.quantity_reserved or 0)
            + int(lot.quantity_damaged or 0)
            if lot is not None
            else Decimal(str(item.quantity or 0))
        )
        if quantity > 0:
            remaining.append(item)
    return remaining


def _pallet_merge_signature(
    pallet: InventoryPallet,
) -> tuple[int, str, list[InventoryPalletItem]]:
    items = _remaining_pallet_items(pallet)
    if not items:
        raise Floor3LocationError("栈板没有可合并的剩余货物", status_code=409)
    customer_ids = {item.customer_id for item in items}
    if None in customer_ids or len(customer_ids) != 1:
        raise Floor3LocationError(
            "栈板客户归属不唯一，不能执行零散货合并", status_code=409
        )
    inventory_types = {
        item.inventory_lot.inventory_type
        if item.inventory_lot is not None
        else item.item_type
        for item in items
    }
    if len(inventory_types) != 1:
        raise Floor3LocationError(
            "栈板库存类型不唯一，不能执行零散货合并", status_code=409
        )
    return int(next(iter(customer_ids))), next(iter(inventory_types)), items


def strict_pallet_merge_profile(pallet: InventoryPallet) -> PalletMergeProfile:
    """Return the compatibility key for one fully authoritative pallet.

    P1-49C deliberately keeps every lot distinct.  This profile only proves
    that changing the container is safe; it never combines quantity or lot
    history.
    """

    if (
        pallet.status != "active"
        or not pallet.is_current
        or pallet.location_id is None
    ):
        raise Floor3LocationError("栈板当前不是可合并的在用系统栈板", status_code=409)
    items = _remaining_pallet_items(pallet)
    if not items or len(items) != len(pallet.items):
        raise Floor3LocationError("栈板含空明细或已失效明细，不能合并", status_code=409)

    customer_ids: set[int] = set()
    inventory_types: set[str] = set()
    units: set[str] = set()
    lot_statuses: set[str] = set()
    for item in items:
        lot = item.inventory_lot
        detail = lot.finished_detail if lot is not None else None
        product = item.product
        if (
            item.item_type != "finished"
            or item.match_status != "matched"
            or item.inventory_lot_id is None
            or lot is None
            or lot.inventory_type != "finished"
            or lot.status not in {"active", "frozen"}
            or detail is None
            or product is None
            or lot.warehouse_location_id != pallet.location_id
            or int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0) <= 0
            or int(lot.quantity_damaged or 0) > 0
            or int(lot.quantity_scrapped or 0) > 0
            or item.customer_id is None
            or item.product_id is None
            or item.customer_id != detail.owner_customer_id
            or item.product_id != detail.product_id
            or product.customer_id != detail.owner_customer_id
            or item.unit != lot.unit
        ):
            raise Floor3LocationError(
                "整板合并只允许客户、产品、位置与质量状态一致的正式成品批次",
                status_code=409,
            )
        customer_ids.add(int(item.customer_id))
        inventory_types.add(str(lot.inventory_type))
        units.add(str(lot.unit))
        lot_statuses.add(str(lot.status))

    if len(customer_ids) != 1:
        raise Floor3LocationError("栈板客户归属不唯一，不能合并", status_code=409)
    if len(inventory_types) != 1:
        raise Floor3LocationError("栈板库存类型不唯一，不能合并", status_code=409)
    if len(units) != 1:
        raise Floor3LocationError("栈板原生单位不唯一，不能合并", status_code=409)
    if len(lot_statuses) != 1:
        raise Floor3LocationError("栈板质量状态不一致，不能合并", status_code=409)
    return PalletMergeProfile(
        customer_id=next(iter(customer_ids)),
        inventory_type=next(iter(inventory_types)),
        unit=next(iter(units)),
        lot_status=next(iter(lot_statuses)),
    )


def lock_pallet_inventory_lots(
    db: Session,
    pallet_ids: list[int] | tuple[int, ...],
    *,
    expected_pallets: dict[int, tuple[int, int]],
    expected_lots: dict[int, tuple],
) -> list[InventoryLot]:
    """Claim every authoritative lot before a deterministic pallet write.

    PostgreSQL honours ``FOR UPDATE`` here, while SQLite does not.  The
    version-preserving CAS update is therefore intentional: on SQLite the
    first statement acquires the database write lock, and on every backend it
    proves that no reservation/release changed the lot after preflight.
    """

    normalized = sorted({int(value) for value in pallet_ids})
    if not normalized:
        return []
    try:
        for pallet_id in sorted(expected_pallets):
            expected_version, expected_location_id = expected_pallets[pallet_id]
            claimed = db.execute(
                update(InventoryPallet)
                .where(
                    InventoryPallet.id == pallet_id,
                    InventoryPallet.version == expected_version,
                    InventoryPallet.status == "active",
                    InventoryPallet.is_current.is_(True),
                    InventoryPallet.location_id == expected_location_id,
                )
                .values(
                    version=InventoryPallet.version,
                    updated_at=InventoryPallet.updated_at,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise Floor3LocationError(
                    "栈板在合并预检期间发生变化，请刷新后重试",
                    status_code=409,
                )

        for lot_id in sorted(expected_lots):
            (
                expected_version,
                expected_location_id,
                expected_inventory_type,
                expected_status,
                expected_unit,
                expected_available,
                expected_reserved,
                expected_consumed,
                expected_damaged,
                expected_scrapped,
                expected_last_movement_at,
            ) = expected_lots[lot_id]
            claimed = db.execute(
                update(InventoryLot)
                .where(
                    InventoryLot.id == lot_id,
                    InventoryLot.version == expected_version,
                    InventoryLot.warehouse_location_id == expected_location_id,
                    InventoryLot.inventory_type == expected_inventory_type,
                    InventoryLot.status == expected_status,
                    InventoryLot.unit == expected_unit,
                    InventoryLot.quantity_available == expected_available,
                    InventoryLot.quantity_reserved == expected_reserved,
                    InventoryLot.quantity_consumed == expected_consumed,
                    InventoryLot.quantity_damaged == expected_damaged,
                    InventoryLot.quantity_scrapped == expected_scrapped,
                    InventoryLot.last_movement_at == expected_last_movement_at,
                )
                .values(
                    version=InventoryLot.version,
                    updated_at=InventoryLot.updated_at,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise Floor3LocationError(
                    "库存批次在合并预检期间发生变化，请刷新后重试",
                    status_code=409,
                )
    except OperationalError as error:
        original = getattr(error, "orig", None)
        sqlite_code = getattr(original, "sqlite_errorcode", None)
        message = str(original or error).lower()
        if sqlite_code not in {5, 6} and not any(
            marker in message for marker in ("locked", "busy")
        ):
            raise
        raise Floor3LocationError(
            "库存批次正被其他操作处理，请稍后刷新重试",
            status_code=409,
        ) from error

    db.expire_all()
    return list(
        db.scalars(
            select(InventoryLot)
            .join(
                InventoryPalletItem,
                InventoryPalletItem.inventory_lot_id == InventoryLot.id,
            )
            .where(InventoryPalletItem.pallet_id.in_(normalized))
            .order_by(InventoryLot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def load_mergeable_pallet(
    db: Session,
    pallet_id: int,
    *,
    require_published_location: bool = True,
) -> tuple[InventoryPallet, PalletMergeProfile]:
    """Load one authoritative pallet and prove its current map location is usable."""

    pallet = _pallet(
        db,
        pallet_id,
        allow_non_operational_source=True,
    )
    profile = strict_pallet_merge_profile(pallet)
    assert pallet.location_id is not None
    location = _operational_pallet_location(
        db,
        pallet.location_id,
        require_published=require_published_location,
        required_inventory_type=profile.inventory_type,
    )
    supported_source = bool(
        (location.source_version == "V11" and location.warehouse_floor == 3)
        or (
            location.source_version == "TWIN_V1"
            and location.warehouse_floor in {1, 3}
        )
        or (
            location.source_version == "P1-25C"
            and location.warehouse_floor == 1
            and location.location_code == "F1-DISPATCH-01"
        )
    )
    if not supported_source:
        raise Floor3LocationError(
            "栈板来源不属于已接入的一楼或三楼正式地图库位",
            status_code=409,
        )
    return pallet, profile


def _idempotent_merge_result(
    db: Session,
    source_movement: InventoryLocationMovement,
    *,
    source_pallet_id: int,
    target_pallet_id: int,
    expected_source_version: int,
    expected_target_version: int,
    idempotency_key: str,
) -> Floor3MergeResult:
    target_movement = _movement_by_idempotency_key(
        db, _merge_target_idempotency_key(idempotency_key)
    )
    if (
        source_movement.movement_type != "clear"
        or source_movement.pallet_id != source_pallet_id
        or source_movement.pallet_version_before != expected_source_version
        or target_movement is None
        or target_movement.movement_type != "add_item"
        or target_movement.pallet_id != target_pallet_id
        or target_movement.pallet_version_before != expected_target_version
        or source_movement.to_location_id != target_movement.to_location_id
    ):
        raise Floor3LocationError("幂等键已用于不同的栈板合并业务", status_code=409)
    return Floor3MergeResult(
        source_pallet=_pallet(db, source_pallet_id, refresh=True),
        target_pallet=_pallet(db, target_pallet_id, refresh=True),
        source_movement=source_movement,
        target_movement=target_movement,
        moved_item_count=0,
        replayed=True,
    )


def merge_pallet_remaining_goods(
    db: Session,
    *,
    source_pallet_id: int,
    target_pallet_id: int,
    expected_source_version: int,
    expected_target_version: int,
    operator_id: int | None,
    idempotency_key: str,
    expected_profile: PalletMergeProfile | None = None,
    require_published_locations: bool = False,
) -> Floor3MergeResult:
    """Move every remaining item to one compatible pallet and release the source."""
    existing = _movement_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return _idempotent_merge_result(
            db,
            existing,
            source_pallet_id=source_pallet_id,
            target_pallet_id=target_pallet_id,
            expected_source_version=expected_source_version,
            expected_target_version=expected_target_version,
            idempotency_key=idempotency_key,
        )
    if source_pallet_id == target_pallet_id:
        raise Floor3LocationError("源栈板和目标栈板不能相同")

    source = _pallet(db, source_pallet_id)
    target = _pallet(db, target_pallet_id)
    if not source.is_current or source.location_id is None:
        raise Floor3LocationError("源栈板已释放，不能再次合并", status_code=409)
    if not target.is_current or target.location_id is None:
        raise Floor3LocationError("目标栈板已释放，不能接收货物", status_code=409)
    source_location = _operational_pallet_location(
        db,
        source.location_id,
        require_published=require_published_locations,
        required_inventory_type=(expected_profile.inventory_type if expected_profile else None),
    )
    target_location = _operational_pallet_location(
        db,
        target.location_id,
        require_published=require_published_locations,
        required_inventory_type=(expected_profile.inventory_type if expected_profile else None),
    )
    if source_location.storage_type == "rack" or target_location.storage_type == "rack":
        raise Floor3LocationError("零散货合并只适用于真实木栈板", status_code=409)

    source_customer, source_type, moved_items = _pallet_merge_signature(source)
    target_customer, target_type, target_items = _pallet_merge_signature(target)
    if expected_profile is not None:
        if strict_pallet_merge_profile(source) != expected_profile:
            raise Floor3LocationError(
                "源栈板内容已变化或与本次合并条件不兼容", status_code=409
            )
        if strict_pallet_merge_profile(target) != expected_profile:
            raise Floor3LocationError(
                "目标栈板内容已变化或与本次合并条件不兼容", status_code=409
            )
    if source_customer != target_customer:
        raise Floor3LocationError("只能合并同一客户的零散货", status_code=409)
    if source_type != target_type:
        raise Floor3LocationError("只能合并同一库存类型的零散货", status_code=409)

    try:
        claims = sorted(
            (
                (source, expected_source_version),
                (target, expected_target_version),
            ),
            key=lambda entry: entry[0].id,
        )
        _claim_pallet_version(db, claims[0][0], expected_version=claims[0][1])
        existing = _movement_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return _idempotent_merge_result(
                db,
                existing,
                source_pallet_id=source_pallet_id,
                target_pallet_id=target_pallet_id,
                expected_source_version=expected_source_version,
                expected_target_version=expected_target_version,
                idempotency_key=idempotency_key,
            )
        _claim_pallet_version(db, claims[1][0], expected_version=claims[1][1])

        source_version_after = expected_source_version + 1
        target_version_after = expected_target_version + 1
        system_note = (
            f"P1-16E-2零散货合并：{source.pallet_code} → {target.pallet_code}"
        )
        now = beijing_now_naive()
        lot_now = utc_now_naive()
        from app.services.warehouse_inventory import (
            record_location_transfer_without_quantity_change,
        )

        for item in moved_items:
            item.pallet = target
            lot = item.inventory_lot
            if lot is not None:
                lot.warehouse_location_id = target_location.id
                lot.version += 1
                lot.last_movement_at = lot_now
                record_location_transfer_without_quantity_change(
                    db,
                    lot=lot,
                    operator_id=operator_id,
                    idempotency_key=f"{idempotency_key}:lot:{lot.id}",
                    remarks=system_note,
                )

        # The item relationship is authoritative.  Flush it before any
        # downstream helper can query the pallet/lot projection in this same
        # transaction (for example a deliberately injected second-step check).
        db.flush()

        source.location_id = None
        source.status = "closed"
        source.is_current = False
        source.needs_relocation = False
        source.closed_at = now
        source.updated_by = operator_id
        target.status = "active"
        target.needs_relocation = _needs_relocation(
            target_location, [*target_items, *moved_items]
        )
        target.updated_by = operator_id
        from app.services.warehouse_ground_slots import (
            release_ground_occupancy_for_pallet,
        )

        release_ground_occupancy_for_pallet(
            db,
            pallet_id=int(source.id),
            operator_id=operator_id,
        )
        db.flush()
        if str(target_location.source_version or "").strip().upper() == "TWIN_V1":
            from app.services.warehouse_inventory import (
                _ensure_finished_projection_postcondition,
            )

            for lot in _linked_inventory_lots(db, target.id):
                _ensure_finished_projection_postcondition(
                    db,
                    lot=lot,
                    operator_id=operator_id,
                    create_missing=True,
                )

        source_movement = InventoryLocationMovement(
            pallet_id=source.id,
            from_location_id=source_location.id,
            to_location_id=target_location.id,
            movement_type="clear",
            operator_id=operator_id,
            moved_at=now,
            idempotency_key=idempotency_key,
            confirmed_at=now,
            pallet_version_before=expected_source_version,
            pallet_version_after=source_version_after,
            remarks=system_note,
        )
        target_movement = InventoryLocationMovement(
            pallet_id=target.id,
            from_location_id=target_location.id,
            to_location_id=target_location.id,
            movement_type="add_item",
            operator_id=operator_id,
            moved_at=now,
            idempotency_key=_merge_target_idempotency_key(idempotency_key),
            confirmed_at=now,
            pallet_version_before=expected_target_version,
            pallet_version_after=target_version_after,
            remarks=system_note,
        )
        db.add_all([source_movement, target_movement])
        db.flush()
    except (Floor3LocationError, IntegrityError):
        existing = _movement_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return _idempotent_merge_result(
                db,
                existing,
                source_pallet_id=source_pallet_id,
                target_pallet_id=target_pallet_id,
                expected_source_version=expected_source_version,
                expected_target_version=expected_target_version,
                idempotency_key=idempotency_key,
            )
        raise

    return Floor3MergeResult(
        source_pallet=_pallet(db, source.id, refresh=True),
        target_pallet=_pallet(db, target.id, refresh=True),
        source_movement=source_movement,
        target_movement=target_movement,
        moved_item_count=len(moved_items),
        replayed=False,
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
    require_published_target: bool = False,
    expected_target_layout_version: int | None = None,
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
        row = _pallet(
            db,
            pallet_id,
            allow_non_operational_source=True,
        )
        if not row.is_current or row.location_id is None:
            raise Floor3LocationError(
                "栈板当前不在有效货位，不能移位", status_code=409
            )
        if row.location_id == to_location_id:
            raise Floor3LocationError("目标货位与当前货位相同")
        source = db.get(WarehouseLocation, row.location_id)
        if source is None:
            raise Floor3LocationError("栈板所在库位不存在", status_code=409)
        if source.storage_type == "rack":
            raise Floor3LocationError("真实木栈板不能从货架格移出", status_code=409)
        target = db.get(WarehouseLocation, to_location_id)
        if target is None:
            raise Floor3LocationError("目标货位不存在", status_code=404)
        if target.storage_type == "rack":
            raise Floor3LocationError("真实木栈板不能移入货架格", status_code=409)
        issue = operational_location_issue(
            db,
            target,
            warehouse_types={"finished", "shared"},
            pallet_storage_only=True,
            require_published=require_published_target,
            require_map_geometry=require_published_target,
            required_inventory_type=("finished" if require_published_target else None),
            require_empty=require_published_target,
            capacity_source_location_id=(
                int(row.location_id) if require_published_target else None
            ),
        )
        if issue:
            raise Floor3LocationError(f"目标货位不可用：{issue}", status_code=409)
        # Acquire SQLite's writer lock before opening a savepoint. Two deferred
        # read transactions cannot reliably upgrade to writers concurrently.
        _claim_empty_active_location(
            db,
            target,
            require_no_live_inventory=require_published_target,
            expected_layout_version=expected_target_layout_version,
        )
        target = db.get(
            WarehouseLocation,
            int(to_location_id),
            populate_existing=True,
        )
        if target is None:
            raise Floor3LocationError("目标货位不存在", status_code=404)
        post_claim_issue = operational_location_issue(
            db,
            target,
            warehouse_types={"finished", "shared"},
            pallet_storage_only=True,
            require_published=require_published_target,
            require_map_geometry=require_published_target,
            required_inventory_type=(
                "finished" if require_published_target else None
            ),
            require_empty=require_published_target,
            capacity_source_location_id=(
                int(row.location_id) if require_published_target else None
            ),
        )
        if post_claim_issue:
            raise Floor3LocationError(
                f"目标货位不可用：{post_claim_issue}",
                status_code=409,
            )

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
            from app.services.warehouse_ground_slots import (
                release_ground_occupancy_for_pallet,
            )

            release_ground_occupancy_for_pallet(
                db,
                pallet_id=int(row.id),
                operator_id=operator_id,
            )
            row.location_id = target.id
            if row.location_occupancy_key != "PRIMARY":
                row.location_occupancy_key = "PRIMARY"
            row.status = "active"
            row.needs_relocation = _needs_relocation(target, row.items)
            row.updated_by = operator_id
            linked_lots = _linked_inventory_lots(db, row.id)
            for lot in linked_lots:
                lot.warehouse_location_id = target.id
                lot.version += 1
                lot.last_movement_at = utc_now_naive()
            db.flush()
            if str(target.source_version or "").strip().upper() == "TWIN_V1":
                from app.services.warehouse_inventory import (
                    _ensure_finished_projection_postcondition,
                )

                for lot in linked_lots:
                    _ensure_finished_projection_postcondition(
                        db,
                        lot=lot,
                        operator_id=operator_id,
                        create_missing=True,
                    )
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
    idempotency_key: str | None = None,
) -> InventoryPallet:
    existing = (
        _movement_by_idempotency_key(db, idempotency_key)
        if idempotency_key
        else None
    )
    if existing is not None:
        if (
            existing.movement_type != "clear"
            or existing.pallet_id != pallet_id
            or existing.pallet_version_before != expected_version
        ):
            raise Floor3LocationError("幂等键已用于不同的栈板清空业务", status_code=409)
        return _pallet(db, pallet_id, refresh=True)
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
    version_before = row.version
    _claim_pallet_version(db, row, expected_version=expected_version)
    from_location_id = row.location_id
    now = beijing_now_naive()
    row.location_id = None
    row.status = "closed"
    row.is_current = False
    row.needs_relocation = False
    row.closed_at = now
    row.updated_by = operator_id
    from app.services.warehouse_ground_slots import (
        release_ground_occupancy_for_pallet,
    )

    release_ground_occupancy_for_pallet(
        db,
        pallet_id=int(row.id),
        operator_id=operator_id,
    )
    db.add(
        InventoryLocationMovement(
            pallet_id=row.id,
            from_location_id=from_location_id,
            to_location_id=None,
            movement_type="clear",
            operator_id=operator_id,
            moved_at=now,
            remarks=_trim(remarks),
            idempotency_key=idempotency_key,
            confirmed_at=now if idempotency_key else None,
            pallet_version_before=version_before,
            pallet_version_after=row.version,
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
    placement_confirmed: bool = False,
) -> InventoryPallet:
    row = _pallet(db, pallet_id)
    if not row.is_current or row.location_id is None:
        raise Floor3LocationError("栈板已清空或移出，不能修改归位标记", status_code=409)
    location = _location(db, row.location_id)
    if not needs_relocation and location.is_temporary:
        raise Floor3LocationError(
            "当前为过道临放，必须先移动到固定货位后才能确认归位",
            status_code=409,
        )
    if not needs_relocation and _needs_relocation(location, row.items) and not placement_confirmed:
        raise Floor3LocationError(
            "货物类型与货位类型不一致，请在现场核对后使用确认已归位",
            status_code=409,
        )
    _claim_pallet_version(db, row, expected_version=expected_version)
    row.needs_relocation = needs_relocation
    row.updated_by = operator_id
    db.flush()
    return row

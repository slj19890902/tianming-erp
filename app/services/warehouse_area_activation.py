from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import re

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.floor3_locations import (
    _layout_rectangles,
    _suggested_layout_rectangles,
    _validate_layout_geometry,
)


AREA_LOCATION_SOURCE_VERSION = "TWIN_V1"
FORMAL_INVENTORY_USAGES = frozenset({"finished", "semi_finished"})


class WarehouseAreaActivationError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AreaLocationCountResult:
    floor_code: str
    area_code: str
    target_count: int
    active_count: int
    created: tuple[WarehouseLocation, ...]
    enabled: tuple[WarehouseLocation, ...]
    disabled: tuple[WarehouseLocation, ...]


def warehouse_floor_for_code(db: Session, floor_code: str) -> WarehouseFloor | None:
    normalized = floor_code.strip().upper()
    floor = db.scalar(
        select(WarehouseFloor).where(WarehouseFloor.floor_code == normalized)
    )
    if floor is not None:
        return floor
    match = re.fullmatch(r"(?:F)?(\d+)(?:F)?", normalized)
    if match is None:
        return None
    return db.scalar(
        select(WarehouseFloor).where(
            WarehouseFloor.floor_number == int(match.group(1))
        )
    )


def policy_inventory_types(policy: WarehouseAreaStoragePolicy) -> list[str]:
    try:
        values = json.loads(policy.allowed_inventory_types_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise WarehouseAreaActivationError(
            "区域正式存放策略已损坏，请停止操作并联系管理员", status_code=409
        ) from error
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise WarehouseAreaActivationError(
            "区域正式存放策略已损坏，请停止操作并联系管理员", status_code=409
        )
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def location_warehouse_type(policy: WarehouseAreaStoragePolicy) -> str | None:
    values = set(policy_inventory_types(policy)) & FORMAL_INVENTORY_USAGES
    if values == FORMAL_INVENTORY_USAGES:
        return "shared"
    if "finished" in values:
        return "finished"
    if "semi_finished" in values:
        return "semi_finished"
    return None


def location_storage_type(policy: WarehouseAreaStoragePolicy) -> str:
    if policy.storage_layout == "rack":
        return "rack"
    # A mixed area can contain separately modelled racks; automatically
    # generated positions are ground/pallet positions and never fake rack bays.
    return "ground"


def formal_area_location_rows(
    db: Session,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea,
) -> list[WarehouseLocation]:
    return list(
        db.scalars(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.floor3_layout))
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
            )
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.id)
        ).all()
    )


def policy_location_transition_blockers(
    db: Session,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea,
    policy: WarehouseAreaStoragePolicy,
) -> list[str]:
    rows = formal_area_location_rows(db, floor=floor, area=area)
    if not rows:
        return []
    location_ids = [row.id for row in rows]
    desired_type = location_warehouse_type(policy)
    live_lot_types = set(
        db.scalars(
            select(InventoryLot.inventory_type)
            .where(
                InventoryLot.warehouse_location_id.in_(location_ids),
                InventoryLot.status.in_(("active", "frozen")),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
            .distinct()
        ).all()
    )
    current_pallet_location_ids = set(
        db.scalars(
            select(InventoryPallet.location_id).where(
                InventoryPallet.location_id.in_(location_ids),
                InventoryPallet.is_current.is_(True),
            )
        ).all()
    )
    blockers: list[str] = []
    if desired_type is None:
        if live_lot_types or current_pallet_location_ids:
            blockers.append("仍有库存或实体栈板，不能改为原料/资产/临时周转用途")
        return blockers
    if desired_type == "shared":
        return blockers
    incompatible_lot_types = live_lot_types - {desired_type}
    if incompatible_lot_types:
        blockers.append("仍有与新用途不一致的成品或半成品库存")
    unverified_pallets = current_pallet_location_ids & {
        row.id for row in rows if row.warehouse_type != desired_type
    }
    if unverified_pallets:
        blockers.append("仍有无法证明与新用途一致的实体栈板")
    return blockers


def formal_area(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
) -> tuple[WarehouseFloor, WarehouseArea, WarehouseAreaStoragePolicy]:
    normalized_floor = floor_code.strip().upper()
    normalized_area = area_code.strip().upper()
    floor = warehouse_floor_for_code(db, normalized_floor)
    if floor is None:
        raise WarehouseAreaActivationError("正式仓库楼层不存在", status_code=404)
    area = db.scalar(
        select(WarehouseArea)
        .options(selectinload(WarehouseArea.storage_policy))
        .where(
            WarehouseArea.floor_id == floor.id,
            WarehouseArea.area_code == normalized_area,
        )
    )
    if area is None:
        raise WarehouseAreaActivationError("正式仓库区域不存在", status_code=404)
    if area.storage_policy is None:
        raise WarehouseAreaActivationError(
            "区域尚未绑定正式存放策略，请先在区域设置中保存", status_code=409
        )
    return floor, area, area.storage_policy


def _active_pallet_exists(location_id: int):
    return select(InventoryPallet.id).where(
        InventoryPallet.location_id == location_id,
        InventoryPallet.is_current.is_(True),
    ).exists()


def _active_inventory_exists(location_id: int):
    return select(InventoryLot.id).where(
        InventoryLot.warehouse_location_id == location_id,
        InventoryLot.status.in_(("active", "frozen")),
        (
            InventoryLot.quantity_available
            + InventoryLot.quantity_reserved
            + InventoryLot.quantity_damaged
        )
        > 0,
    ).exists()


def _location_serial(floor_code: str, area_code: str, location_code: str) -> int | None:
    match = re.fullmatch(
        rf"{re.escape(floor_code)}-{re.escape(area_code)}-L(\d+)",
        location_code,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _set_location_active(
    db: Session,
    *,
    location: WarehouseLocation,
    is_active: bool,
    operator_id: int,
    expected_version: int | None = None,
) -> WarehouseLocation:
    layout = location.floor3_layout
    if layout is None:
        raise WarehouseAreaActivationError("区域库位缺少布局记录", status_code=409)
    if expected_version is not None and layout.version != expected_version:
        raise WarehouseAreaActivationError(
            "布局已被其他操作更新，请刷新后重试", status_code=409
        )
    if not is_active and (
        db.scalar(select(_active_pallet_exists(location.id)))
        or db.scalar(select(_active_inventory_exists(location.id)))
    ):
        raise WarehouseAreaActivationError(
            "库位仍有库存、预占或实体栈板，不能停用", status_code=409
        )
    location.is_active = is_active
    layout.version += 1
    layout.updated_by = operator_id
    layout.updated_at = beijing_now_naive()
    db.flush()
    return location


def set_area_location_active(
    db: Session,
    *,
    location_id: int,
    is_active: bool,
    expected_version: int,
    operator_id: int,
) -> WarehouseLocation:
    location = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(
            WarehouseLocation.id == location_id,
            WarehouseLocation.source_version == AREA_LOCATION_SOURCE_VERSION,
        )
    )
    if location is None:
        raise WarehouseAreaActivationError("正式区域库位不存在", status_code=404)
    floor, area, policy = formal_area(
        db,
        floor_code=f"{location.warehouse_floor}F",
        area_code=str(location.area_code or ""),
    )
    if location.is_active == is_active:
        raise WarehouseAreaActivationError("库位已处于该状态", status_code=409)
    result = _set_location_active(
        db,
        location=location,
        is_active=is_active,
        operator_id=operator_id,
        expected_version=expected_version,
    )
    active_count = int(
        db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                WarehouseLocation.area_code == area.area_code,
                WarehouseLocation.source_version == AREA_LOCATION_SOURCE_VERSION,
                WarehouseLocation.is_active.is_(True),
            )
        )
        or 0
    )
    area.planned_location_count = active_count
    area.construction_status = "layout_building"
    policy.status = "draft"
    policy.published_map_revision = None
    policy.version += 1
    policy.updated_by = operator_id
    db.flush()
    return result


def adjust_area_location_count(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
    target_count: int,
    operator_id: int,
) -> AreaLocationCountResult:
    if target_count < 0 or target_count > 500:
        raise WarehouseAreaActivationError("目标库位数必须在 0 到 500 之间")
    floor, area, policy = formal_area(
        db, floor_code=floor_code, area_code=area_code
    )
    warehouse_type = location_warehouse_type(policy)
    if target_count and warehouse_type is None:
        raise WarehouseAreaActivationError(
            "原料、模具、印版或临时周转区使用各自台账，不生成成品/半成品库存库位",
            status_code=409,
        )
    normalized_floor = floor.floor_code.upper()
    normalized_area = area.area_code.upper()
    all_rows = list(
        db.scalars(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.floor3_layout))
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                WarehouseLocation.source_version == AREA_LOCATION_SOURCE_VERSION,
                func.upper(WarehouseLocation.area_code) == normalized_area,
            )
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.id)
        ).all()
    )
    active_rows = [row for row in all_rows if row.is_active]
    current_count = len(active_rows)
    created: list[WarehouseLocation] = []
    enabled: list[WarehouseLocation] = []
    disabled: list[WarehouseLocation] = []

    if target_count > current_count:
        needed = target_count - current_count
        reusable = [
            row
            for row in all_rows
            if not row.is_active
            and row.floor3_layout is not None
            and not db.scalar(select(_active_pallet_exists(row.id)))
            and not db.scalar(select(_active_inventory_exists(row.id)))
        ]
        for row in reusable[:needed]:
            enabled.append(
                _set_location_active(
                    db, location=row, is_active=True, operator_id=operator_id
                )
            )
        needed -= len(enabled)
        if needed:
            occupied = _layout_rectangles([*active_rows, *enabled])
            layouts = _suggested_layout_rectangles(
                target_count=target_count,
                needed=needed,
                occupied=occupied,
            )
            existing_serials = {
                value
                for row in all_rows
                if (
                    value := _location_serial(
                        normalized_floor, normalized_area, row.location_code
                    )
                )
                is not None
            }
            next_serial = max(existing_serials, default=0) + 1
            next_sort = int(
                db.scalar(select(func.max(WarehouseLocation.sort_order))) or 0
            ) + 1
            for left, top, width, height in layouts:
                while next_serial in existing_serials:
                    next_serial += 1
                code = f"{normalized_floor}-{normalized_area}-L{next_serial:03d}"
                row = WarehouseLocation(
                    location_code=code,
                    location_name=f"{area.area_name} {next_serial:03d} 号位",
                    warehouse_type=warehouse_type,
                    warehouse_floor=floor.floor_number,
                    area_code=normalized_area,
                    storage_type=location_storage_type(policy),
                    sort_order=next_sort,
                    is_temporary=False,
                    source_version=AREA_LOCATION_SOURCE_VERSION,
                    placement_status="unplaced",
                )
                row.floor3_layout = Floor3LocationLayout(
                    left_pct=left,
                    top_pct=top,
                    width_pct=width,
                    height_pct=height,
                    z_index=0,
                    version=1,
                    source_type="manual",
                    created_by=operator_id,
                    updated_by=operator_id,
                )
                db.add(row)
                db.flush()
                created.append(row)
                existing_serials.add(next_serial)
                next_serial += 1
                next_sort += 1
    elif target_count < current_count:
        needed = current_count - target_count
        removable = [
            row
            for row in reversed(active_rows)
            if row.floor3_layout is not None
            and not db.scalar(select(_active_pallet_exists(row.id)))
            and not db.scalar(select(_active_inventory_exists(row.id)))
        ]
        if len(removable) < needed:
            raise WarehouseAreaActivationError(
                f"只能减少 {len(removable)} 个真正空库位；有库存、预占或实体栈板的库位不会被移除",
                status_code=409,
            )
        for row in removable[:needed]:
            disabled.append(
                _set_location_active(
                    db, location=row, is_active=False, operator_id=operator_id
                )
            )

    area.planned_location_count = target_count
    area.construction_status = "layout_building"
    policy.status = "draft"
    policy.published_map_revision = None
    policy.version += 1
    policy.updated_by = operator_id
    db.flush()
    return AreaLocationCountResult(
        floor_code=normalized_floor,
        area_code=normalized_area,
        target_count=target_count,
        active_count=target_count,
        created=tuple(created),
        enabled=tuple(enabled),
        disabled=tuple(disabled),
    )


def update_area_location_layout(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
    slots: list[dict],
    operator_id: int,
) -> list[Floor3LocationLayout]:
    floor, area, policy = formal_area(
        db, floor_code=floor_code, area_code=area_code
    )
    if len({slot["location_id"] for slot in slots}) != len(slots):
        raise WarehouseAreaActivationError("批量布局不能重复同一库位")
    for slot in slots:
        _validate_layout_geometry(
            left_pct=slot["left_pct"],
            top_pct=slot["top_pct"],
            width_pct=slot["width_pct"],
            height_pct=slot["height_pct"],
        )
    layouts: list[Floor3LocationLayout] = []
    for slot in slots:
        layout = db.scalar(
            select(Floor3LocationLayout)
            .join(WarehouseLocation)
            .where(
                Floor3LocationLayout.location_id == slot["location_id"],
                WarehouseLocation.warehouse_floor == floor.floor_number,
                WarehouseLocation.source_version == AREA_LOCATION_SOURCE_VERSION,
                func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
            )
        )
        if layout is None:
            raise WarehouseAreaActivationError(
                "布局库位不存在或不属于该正式区域", status_code=404
            )
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
            raise WarehouseAreaActivationError(
                "布局已被其他操作更新，请刷新后重试", status_code=409
            )
        db.execute(
            update(WarehouseLocation)
            .where(WarehouseLocation.id == slot["location_id"])
            .values(placement_status="placed")
            .execution_options(synchronize_session=False)
        )
        layouts.append(layout)
    db.flush()
    active_rows = list(
        db.scalars(
            select(WarehouseLocation).where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                WarehouseLocation.area_code == area.area_code,
                WarehouseLocation.source_version == AREA_LOCATION_SOURCE_VERSION,
                WarehouseLocation.is_active.is_(True),
            ).execution_options(populate_existing=True)
        ).all()
    )
    area.construction_status = (
        "layout_complete"
        if len(active_rows) == area.planned_location_count
        and all(row.placement_status == "placed" for row in active_rows)
        else "layout_building"
    )
    policy.status = "draft"
    policy.published_map_revision = None
    policy.version += 1
    policy.updated_by = operator_id
    db.flush()
    return list(
        db.scalars(
            select(Floor3LocationLayout)
            .where(
                Floor3LocationLayout.location_id.in_(
                    [slot["location_id"] for slot in slots]
                )
            )
            .order_by(Floor3LocationLayout.location_id)
            .execution_options(populate_existing=True)
        ).all()
    )


def publish_floor_area_policies(
    db: Session,
    *,
    floor_code: str,
    published_revision: str,
    operator_id: int,
    published_features: list[dict],
) -> list[WarehouseAreaStoragePolicy]:
    floor = warehouse_floor_for_code(db, floor_code)
    if floor is None:
        return []
    policies = list(
        db.scalars(
            select(WarehouseAreaStoragePolicy)
            .join(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseAreaStoragePolicy.area))
        ).all()
    )
    features = {
        str(item.get("id") or ""): item
        for item in published_features
        if item.get("feature_kind") == "zone" and item.get("id")
    }
    published: list[WarehouseAreaStoragePolicy] = []
    for policy in policies:
        area = policy.area
        feature = features.get(policy.map_feature_id)
        if (
            feature is None
            or str(feature.get("erp_area_code") or "").strip().upper()
            != area.area_code.upper()
            or list(feature.get("allowed_inventory_types") or [])
            != policy_inventory_types(policy)
            or str(feature.get("storage_layout") or "") != policy.storage_layout
        ):
            continue
        location_type = location_warehouse_type(policy)
        transition_blockers = policy_location_transition_blockers(
            db,
            floor=floor,
            area=area,
            policy=policy,
        )
        if transition_blockers:
            raise WarehouseAreaActivationError(
                f"{area.area_code} 区" + "；".join(transition_blockers),
                status_code=409,
            )
        if location_type is not None and area.planned_location_count > 0:
            source_version = (
                "V11" if floor.floor_number == 3 else AREA_LOCATION_SOURCE_VERSION
            )
            active_rows = list(
                db.scalars(
                    select(WarehouseLocation).where(
                        WarehouseLocation.warehouse_floor == floor.floor_number,
                        WarehouseLocation.area_code == area.area_code,
                        WarehouseLocation.source_version == source_version,
                        WarehouseLocation.is_active.is_(True),
                    )
                ).all()
            )
            if len(active_rows) != area.planned_location_count or any(
                row.placement_status != "placed" for row in active_rows
            ):
                raise WarehouseAreaActivationError(
                    f"{area.area_code} 区仍有库位未完成布局，不能发布投入使用",
                    status_code=409,
                )
        area_rows = formal_area_location_rows(db, floor=floor, area=area)
        if location_type is None:
            for row in area_rows:
                row.is_active = False
            area.planned_location_count = 0
        else:
            for row in area_rows:
                row.warehouse_type = location_type
        policy.status = "published"
        policy.published_map_revision = published_revision
        policy.version += 1
        policy.updated_by = operator_id
        area.construction_status = "enabled"
        published.append(policy)
    db.flush()
    return published

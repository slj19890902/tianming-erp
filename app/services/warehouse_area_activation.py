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
    FLOOR3_LAYOUT_AREA_CODES,
    _layout_rectangles,
    _suggested_layout_rectangles,
    _validate_layout_geometry,
)


AREA_LOCATION_SOURCE_VERSION = "TWIN_V1"
FORMAL_INVENTORY_USAGES = frozenset({"finished", "semi_finished"})
AREA_POLICY_INVENTORY_USAGES = frozenset(
    {
        "finished",
        "semi_finished",
        "raw_material",
        "mold",
        "print_plate",
        "temporary_turnover",
    }
)
AREA_LOCATION_MANAGEMENT_ACTIONS = (
    "location_count",
    "layout",
    "auto_arrange",
    "disable_empty",
    "enable_empty",
)
_FLOOR3_V11_MEASURED_FEATURE_CODES = {
    "E4": "ZONE-3F-FG-001",
}


def floor3_v11_map_binding_is_proven(
    db: Session,
    *,
    floor: WarehouseFloor,
    feature: dict,
    area: WarehouseArea | None,
    feature_area_code_count: int,
) -> bool:
    """Recognize the already-verified measured-map identity of legacy 3F areas.

    A1--F34 predate ``warehouse_area_storage_policies`` but their V11 location
    identities were created from the same measured-map zones.  They must not
    be treated as unrelated unbound areas when a different 3F zone is being
    confirmed.  The proof is deliberately narrow: exact legacy feature code,
    one same-code zone, one enabled same-floor ledger area, and only V11
    physical locations with at least one active position.
    """

    if floor.floor_number != 3 or area is None or area.floor_id != floor.id:
        return False
    area_code = str(feature.get("erp_area_code") or "").strip().upper()
    if (
        not area_code
        or area_code not in FLOOR3_LAYOUT_AREA_CODES
        or feature.get("feature_kind") != "zone"
        or str(feature.get("feature_code") or "").strip().upper()
        != _FLOOR3_V11_MEASURED_FEATURE_CODES.get(
            area_code, f"ZONE-3F-ERP-{area_code}"
        )
        or feature_area_code_count != 1
        or area.area_code.upper() != area_code
        or area.construction_status != "enabled"
        or area.storage_policy is not None
    ):
        return False
    rows = list(
        db.scalars(
            select(WarehouseLocation).where(
                WarehouseLocation.warehouse_floor == 3,
                func.upper(WarehouseLocation.area_code) == area_code,
            )
        ).all()
    )
    return bool(
        rows
        and all(row.source_version == "V11" for row in rows)
        and any(row.is_active for row in rows)
    )


def legacy_v11_name_only_change_is_safe(
    db: Session,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea | None,
    feature: dict,
    feature_area_code_count: int,
    requested_inventory_types: list[str],
    requested_storage_layout: str,
    current_feature: dict | None = None,
) -> bool:
    """Prove a legacy V11 edit changes only its employee-facing area name.

    These 22 measured 3F areas intentionally remain outside the storage-policy
    lifecycle.  Their physical V11 rows are the current usage/layout fact, so a
    rename may bypass policy creation only when the submitted usage and layout
    exactly match both those rows and any explicit values already on the map.
    """

    if not floor3_v11_map_binding_is_proven(
        db,
        floor=floor,
        feature=feature,
        area=area,
        feature_area_code_count=feature_area_code_count,
    ):
        return False
    assert area is not None
    current_policy = legacy_v11_area_policy_projection(
        db,
        floor=floor,
        area=area,
    )
    if current_policy is None:
        return False
    current_inventory_types, current_storage_layout = current_policy
    if (
        set(current_inventory_types) != set(requested_inventory_types)
        or current_storage_layout != requested_storage_layout
    ):
        return False
    if current_feature is not None:
        explicit_types = list(current_feature.get("allowed_inventory_types") or [])
        if explicit_types and set(explicit_types) != set(requested_inventory_types):
            return False
        explicit_layout = str(current_feature.get("storage_layout") or "").strip()
        if explicit_layout and explicit_layout != requested_storage_layout:
            return False
    return True


def legacy_v11_area_policy_projection(
    db: Session,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea,
) -> tuple[list[str], str] | None:
    """Derive the read-only policy fields of one legacy measured V11 area.

    The V11 location ledger remains authoritative for these policy-less areas.
    This projection lets the planning form round-trip the existing facts (for
    example D1's ground plus rack rows become ``mixed``) without materializing
    a second SQL storage policy.
    """

    rows = formal_area_location_rows(db, floor=floor, area=area)
    if not rows or any(row.source_version != "V11" for row in rows):
        return None
    warehouse_types = {
        str(row.warehouse_type or "").strip() for row in rows
    }
    inventory_types = (
        ["finished"]
        if warehouse_types == {"finished"}
        else ["semi_finished"]
        if warehouse_types == {"semi_finished"}
        else None
    )
    if inventory_types is None:
        return None
    storage_layouts: set[str] = set()
    for row in rows:
        storage_type = str(row.storage_type or "").strip()
        if storage_type == "rack":
            storage_layouts.add("rack")
        elif storage_type in {"ground", "temporary_aisle"}:
            storage_layouts.add("pallet_ground")
        else:
            return None
    storage_layout = (
        next(iter(storage_layouts))
        if len(storage_layouts) == 1
        else "mixed"
        if storage_layouts == {"rack", "pallet_ground"}
        else None
    )
    if storage_layout is None:
        return None
    return inventory_types, storage_layout


class WarehouseAreaActivationError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class PublishedAreaPolicyResult(list[WarehouseAreaStoragePolicy]):
    """Published policies plus non-policy legacy area-name mutations."""

    def __init__(
        self,
        policies: list[WarehouseAreaStoragePolicy],
        *,
        legacy_name_updated_area_codes: list[str] | None = None,
    ) -> None:
        super().__init__(policies)
        self.legacy_name_updated_area_codes = tuple(
            legacy_name_updated_area_codes or []
        )

    @property
    def legacy_name_update_count(self) -> int:
        return len(self.legacy_name_updated_area_codes)


@dataclass(frozen=True)
class AreaLocationCountResult:
    floor_code: str
    area_code: str
    target_count: int
    active_count: int
    created: tuple[WarehouseLocation, ...]
    enabled: tuple[WarehouseLocation, ...]
    disabled: tuple[WarehouseLocation, ...]


@dataclass(frozen=True)
class AreaLocationManagementRoute:
    floor_code: str
    area_code: str
    management_mode: str
    source_version: str
    available_actions: tuple[str, ...] = AREA_LOCATION_MANAGEMENT_ACTIONS


def area_location_management_payload(
    route: AreaLocationManagementRoute,
) -> dict[str, object]:
    return {
        "floor_code": route.floor_code,
        "area_code": route.area_code,
        "management_mode": route.management_mode,
        "source_version": route.source_version,
        "available_actions": list(route.available_actions),
    }


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


def resolve_area_location_management(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
) -> AreaLocationManagementRoute:
    """Resolve the authoritative location lifecycle for one formal area."""

    normalized_area = area_code.strip().upper()
    floor = warehouse_floor_for_code(db, floor_code)
    if floor is None:
        raise WarehouseAreaActivationError("正式仓库楼层不存在", status_code=404)
    if not normalized_area:
        raise WarehouseAreaActivationError("正式仓库区域不存在", status_code=404)

    raw_sources = list(
        db.scalars(
            select(WarehouseLocation.source_version)
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                func.upper(WarehouseLocation.area_code) == normalized_area,
            )
            .distinct()
        ).all()
    )
    source_versions = {
        str(value).strip() for value in raw_sources if str(value or "").strip()
    }
    has_unversioned_rows = any(not str(value or "").strip() for value in raw_sources)
    supported_sources = {"V11", AREA_LOCATION_SOURCE_VERSION}
    if (
        has_unversioned_rows
        or source_versions - supported_sources
        or source_versions == supported_sources
    ):
        raise WarehouseAreaActivationError(
            f"{normalized_area} 区库位来源冲突，请停止操作并核对正式区域台账",
            status_code=409,
        )

    if (
        floor.floor_number == 3
        and normalized_area in FLOOR3_LAYOUT_AREA_CODES
        and AREA_LOCATION_SOURCE_VERSION not in source_versions
    ):
        return AreaLocationManagementRoute(
            floor_code=floor.floor_code.upper(),
            area_code=normalized_area,
            management_mode="floor3_v11",
            source_version="V11",
        )

    if "V11" in source_versions:
        raise WarehouseAreaActivationError(
            f"{normalized_area} 区存在无法归属当前实测三楼区域的库位，请停止操作并核对",
            status_code=409,
        )

    formal_area(db, floor_code=floor.floor_code, area_code=normalized_area)
    return AreaLocationManagementRoute(
        floor_code=floor.floor_code.upper(),
        area_code=normalized_area,
        management_mode="formal_area",
        source_version=AREA_LOCATION_SOURCE_VERSION,
    )


def resolve_location_management(
    db: Session,
    *,
    location_id: int,
) -> tuple[WarehouseLocation, AreaLocationManagementRoute]:
    location = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id == location_id)
    )
    if location is None:
        raise WarehouseAreaActivationError("正式区域库位不存在", status_code=404)
    if location.warehouse_floor is None or not str(location.area_code or "").strip():
        raise WarehouseAreaActivationError("库位尚未绑定正式楼层和区域", status_code=409)
    route = resolve_area_location_management(
        db,
        floor_code=f"{location.warehouse_floor}F",
        area_code=str(location.area_code),
    )
    if location.source_version != route.source_version:
        raise WarehouseAreaActivationError(
            "库位来源与正式区域管理路径不一致，请停止操作并核对",
            status_code=409,
        )
    return location, route


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


def location_warehouse_type_for_inventory_types(values: list[str]) -> str | None:
    values = set(values)
    pallet_inventory_types = values & {
        "finished",
        "semi_finished",
        "raw_material",
    }
    # Raw material remains a pallet snapshot fact rather than a finished/semi
    # InventoryLot.  Its measured floor position therefore uses a shared
    # physical location while the published area policy remains authoritative
    # for the raw-material usage.
    if "raw_material" in pallet_inventory_types:
        return "shared"
    formal_values = pallet_inventory_types & FORMAL_INVENTORY_USAGES
    if formal_values == FORMAL_INVENTORY_USAGES:
        return "shared"
    if "finished" in formal_values:
        return "finished"
    if "semi_finished" in formal_values:
        return "semi_finished"
    return None


def location_warehouse_type(policy: WarehouseAreaStoragePolicy) -> str | None:
    return location_warehouse_type_for_inventory_types(policy_inventory_types(policy))


def location_storage_type_for_layout(storage_layout: str) -> str:
    if storage_layout == 'rack':
        return "rack"
    # A mixed area can contain separately modelled racks; automatically
    # generated positions are ground/pallet positions and never fake rack bays.
    return "ground"


def location_storage_type(policy: WarehouseAreaStoragePolicy) -> str:
    return location_storage_type_for_layout(policy.storage_layout)


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
    requested_inventory_types: list[str] | None = None,
    requested_storage_layout: str | None = None,
) -> list[str]:
    rows = formal_area_location_rows(db, floor=floor, area=area)
    if not rows:
        return []
    location_ids = [row.id for row in rows]
    current_type = location_warehouse_type(policy)
    desired_type = (
        location_warehouse_type_for_inventory_types(requested_inventory_types)
        if requested_inventory_types is not None else current_type
    )
    current_storage = location_storage_type(policy)
    desired_storage = location_storage_type_for_layout(
        requested_storage_layout or policy.storage_layout
    )
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
    occupied = bool(live_lot_types or current_pallet_location_ids)
    if desired_type is None:
        if occupied:
            blockers.append("区域内仍有库存或实体栈板，请先在地图中移到其他已启用区域，再转换用途")
        return blockers
    if desired_type == "shared":
        if desired_storage != current_storage and occupied:
            blockers.append("区域内仍有库存或实体栈板，请先完成移货，再把栈板区与货架区相互转换")
        return blockers
    if desired_type != current_type and occupied:
        blockers.append("区域内仍有库存或实体栈板，请先在地图中移到其他已启用区域，再转换用途")
    if desired_storage != current_storage and occupied:
        blockers.append("区域内仍有库存或实体栈板，请先完成移货，再把栈板区与货架区相互转换")
    incompatible_lot_types = live_lot_types - {desired_type}
    if incompatible_lot_types:
        blockers.append("仍有与新用途不一致的成品或半成品库存")
    unverified_pallets = current_pallet_location_ids & {
        row.id for row in rows if row.warehouse_type != desired_type
    }
    if unverified_pallets:
        blockers.append("仍有无法证明与新用途一致的实体栈板")
    return list(dict.fromkeys(blockers))


def unbound_area_location_transition_blockers(
    db: Session,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea,
    requested_inventory_types: list[str],
    requested_storage_layout: str,
) -> list[str]:
    """Protect occupied locations when a measured zone adopts a formal area."""

    rows = formal_area_location_rows(db, floor=floor, area=area)
    if not rows:
        return []
    location_ids = [row.id for row in rows]
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
    pallet_location_ids = set(
        db.scalars(
            select(InventoryPallet.location_id).where(
                InventoryPallet.location_id.in_(location_ids),
                InventoryPallet.is_current.is_(True),
            )
        ).all()
    )
    if not live_lot_types and not pallet_location_ids:
        return []

    desired_type = location_warehouse_type_for_inventory_types(
        requested_inventory_types
    )
    desired_storage = location_storage_type_for_layout(requested_storage_layout)
    occupied_rows = [
        row
        for row in rows
        if row.id in pallet_location_ids
        or bool(
            db.scalar(
                select(InventoryLot.id)
                .where(
                    InventoryLot.warehouse_location_id == row.id,
                    InventoryLot.status.in_(("active", "frozen")),
                    (
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_damaged
                    )
                    > 0,
                )
                .limit(1)
            )
        )
    ]
    blockers: list[str] = []
    if desired_type is None or (
        desired_type != "shared" and live_lot_types - {desired_type}
    ):
        blockers.append("区域内仍有与新用途不一致的库存，请先在地图中移到其他已启用区域")
    if desired_type not in (None, "shared") and any(
        row.warehouse_type != desired_type for row in occupied_rows
    ):
        blockers.append("区域内仍有与新用途不一致的实体栈板，请先完成移货")
    if any(row.storage_type != desired_storage for row in occupied_rows):
        blockers.append("区域内仍有库存或实体栈板，请先完成移货，再把栈板区与货架区相互转换")
    return list(dict.fromkeys(blockers))


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


def _advance_policy_version(
    db: Session,
    *,
    policy: WarehouseAreaStoragePolicy,
    operator_id: int,
    status: str | None = None,
    published_map_revision: str | None | object = ...,
) -> None:
    expected_version = policy.version
    values: dict[str, object] = {
        "version": expected_version + 1,
        "updated_by": operator_id,
        "updated_at": beijing_now_naive(),
    }
    if status is not None:
        values["status"] = status
    if published_map_revision is not ...:
        values["published_map_revision"] = published_map_revision
    result = db.execute(
        update(WarehouseAreaStoragePolicy)
        .where(
            WarehouseAreaStoragePolicy.id == policy.id,
            WarehouseAreaStoragePolicy.version == expected_version,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise WarehouseAreaActivationError(
            "区域设置已被其他操作更新，请刷新后重试", status_code=409
        )
    db.expire(policy)


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
    location_conditions = [WarehouseLocation.id == location.id]
    if not is_active:
        location_conditions.extend(
            [
                ~_active_pallet_exists(location.id),
                ~_active_inventory_exists(location.id),
            ]
        )
    location_update = db.execute(
        update(WarehouseLocation)
        .where(*location_conditions)
        .values(is_active=is_active)
        .execution_options(synchronize_session=False)
    )
    if location_update.rowcount != 1:
        raise WarehouseAreaActivationError(
            "库位仍有库存、预占或实体栈板，不能停用", status_code=409
        )
    current_version = layout.version
    layout_update = db.execute(
        update(Floor3LocationLayout)
        .where(
            Floor3LocationLayout.id == layout.id,
            Floor3LocationLayout.version == current_version,
        )
        .values(
            version=current_version + 1,
            updated_by=operator_id,
            updated_at=beijing_now_naive(),
        )
        .execution_options(synchronize_session=False)
    )
    if layout_update.rowcount != 1:
        raise WarehouseAreaActivationError(
            "布局已被其他操作更新，请刷新后重试", status_code=409
        )
    db.flush()
    db.expire(location)
    db.expire(layout)
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
    was_published = policy.status == "published"
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
    area.construction_status = "enabled" if was_published else "layout_building"
    _advance_policy_version(
        db,
        policy=policy,
        operator_id=operator_id,
        status="draft" if not was_published else None,
        published_map_revision=None if not was_published else ...,
    )
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
    was_published = policy.status == "published"
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

    def auto_managed_layout(row: WarehouseLocation) -> bool:
        layout = row.floor3_layout
        if layout is None:
            return False
        # Count changes never guess that a historical manual/version-1 row was
        # system generated.  An administrator must first adopt such rows via
        # the dedicated auto-arrange confirmation.
        return layout.source_type == "seeded"

    if target_count == current_count:
        return AreaLocationCountResult(
            floor_code=normalized_floor,
            area_code=normalized_area,
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
                    placement_status="placed" if was_published else "unplaced",
                )
                row.floor3_layout = Floor3LocationLayout(
                    left_pct=left,
                    top_pct=top,
                    width_pct=width,
                    height_pct=height,
                    z_index=0,
                    version=1,
                    source_type="seeded",
                    layout_kind="logical_anchor",
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
            and auto_managed_layout(row)
            and not db.scalar(select(_active_pallet_exists(row.id)))
            and not db.scalar(select(_active_inventory_exists(row.id)))
        ]
        if len(removable) < needed:
            raise WarehouseAreaActivationError(
                f"只能减少 {len(removable)} 个空闲系统货位；占用或尚未明确接管的历史货位不会被移除，请先自动排布确认",
                status_code=409,
            )
        for row in removable[:needed]:
            assert row.floor3_layout is not None
            disabled.append(
                _set_location_active(
                    db, location=row, is_active=False, operator_id=operator_id
                )
            )

    area.planned_location_count = target_count
    area.construction_status = "enabled" if was_published else "layout_building"
    _advance_policy_version(
        db,
        policy=policy,
        operator_id=operator_id,
        status="draft" if not was_published else None,
        published_map_revision=None if not was_published else ...,
    )
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
    was_published = policy.status == "published"
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
                source_type="manual",
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
        "enabled"
        if was_published
        else (
            "layout_complete"
            if len(active_rows) == area.planned_location_count
            and all(row.placement_status == "placed" for row in active_rows)
            else "layout_building"
        )
    )
    _advance_policy_version(
        db,
        policy=policy,
        operator_id=operator_id,
        status="draft" if not was_published else None,
        published_map_revision=None if not was_published else ...,
    )
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
    defer_location_readiness_for_feature_id: str | None = None,
) -> PublishedAreaPolicyResult:
    floor = warehouse_floor_for_code(db, floor_code)
    if floor is None:
        return PublishedAreaPolicyResult([])
    if not str(published_revision or "").strip() or len(str(published_revision)) > 64:
        raise WarehouseAreaActivationError("正式地图修订号无效", status_code=409)
    areas = list(
        db.scalars(
            select(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseArea.storage_policy))
        ).all()
    )
    areas_by_code = {area.area_code.upper(): area for area in areas}
    requested_feature_ids = {
        str(item.get("id") or "").strip()
        for item in published_features
        if item.get("feature_kind") == "zone" and str(item.get("id") or "").strip()
    }
    policies_by_feature = {
        policy.map_feature_id: policy
        for policy in db.scalars(
            select(WarehouseAreaStoragePolicy)
            .where(WarehouseAreaStoragePolicy.map_feature_id.in_(requested_feature_ids))
            .options(selectinload(WarehouseAreaStoragePolicy.area))
        ).all()
    }
    proposals: list[
        tuple[dict, str, str, list[str], str, WarehouseArea | None,
              WarehouseAreaStoragePolicy | None]
    ] = []
    legacy_name_updates: list[
        tuple[dict, str, str, list[str], str, WarehouseArea]
    ] = []
    proposal_area_codes: set[str] = set()
    proposal_feature_ids: set[str] = set()
    feature_area_code_counts: dict[str, int] = {}
    for item in published_features:
        if item.get("feature_kind") != "zone":
            continue
        code = str(item.get("erp_area_code") or "").strip().upper()
        if code:
            feature_area_code_counts[code] = feature_area_code_counts.get(code, 0) + 1
    for feature in published_features:
        if feature.get("feature_kind") != "zone" or not feature.get("id"):
            continue
        feature_id = str(feature["id"]).strip()
        if not feature_id or len(feature_id) > 80:
            raise WarehouseAreaActivationError("地图区域标识无效", status_code=409)
        feature_policy = policies_by_feature.get(feature_id)
        area_code = str(feature.get("erp_area_code") or "").strip().upper()
        if not area_code:
            has_partial_json_policy = any(
                feature.get(key) is not None
                for key in (
                    "erp_area_code",
                    "allowed_inventory_types",
                    "storage_layout",
                    "formal_area_name",
                )
            )
            if has_partial_json_policy:
                raise WarehouseAreaActivationError(
                    "地图区域的正式编号与策略不完整",
                    status_code=409,
                )
            if feature_policy is None:
                continue
            if feature_policy.status != "published":
                # A SQL draft belonging to an unrelated advanced-map draft
                # must not be pulled into this one-zone publish transaction.
                # The selected zone always carries its explicit area code.
                continue
            if (
                feature_policy.area.floor_id != floor.id
            ):
                raise WarehouseAreaActivationError(
                    "地图区域的历史正式绑定不可用于当前楼层",
                    status_code=409,
                )
            feature = {
                **feature,
                "erp_area_code": feature_policy.area.area_code,
                "allowed_inventory_types": policy_inventory_types(feature_policy),
                "storage_layout": feature_policy.storage_layout,
                "formal_area_name": feature_policy.area.area_name,
            }
            area_code = feature_policy.area.area_code.upper()
        area = areas_by_code.get(area_code)
        projected_legacy_binding = floor3_v11_map_binding_is_proven(
            db,
            floor=floor,
            feature=feature,
            area=area,
            feature_area_code_count=feature_area_code_counts.get(area_code, 0),
        )
        has_explicit_policy_change = bool(
            feature.get("allowed_inventory_types")
            or feature.get("formal_area_id") not in (None, "")
            or feature.get("formal_floor_id") not in (None, "")
        )
        if projected_legacy_binding and not has_explicit_policy_change:
            # The legacy V11 zone is already an operational measured-map
            # identity.  Publishing an unrelated new zone must leave it
            # untouched instead of forcing all historical areas to migrate in
            # the same transaction.
            continue
        raw_formal_area_id = feature.get("formal_area_id")
        raw_formal_floor_id = feature.get("formal_floor_id")
        has_formal_area_id = raw_formal_area_id not in (None, "")
        has_formal_floor_id = raw_formal_floor_id not in (None, "")
        if has_formal_area_id != has_formal_floor_id:
            raise WarehouseAreaActivationError(
                f"{area_code} 区域草稿的正式区域身份不完整", status_code=409
            )
        if area_code in proposal_area_codes or feature_id in proposal_feature_ids:
            raise WarehouseAreaActivationError(
                f"{area_code} 区发布草稿存在重复区域或地图标识", status_code=409
            )
        proposal_area_codes.add(area_code)
        proposal_feature_ids.add(feature_id)
        inventory_types = list(
            dict.fromkeys(
                str(value).strip()
                for value in feature.get("allowed_inventory_types") or []
                if str(value).strip()
            )
        )
        storage_layout = str(feature.get("storage_layout") or "").strip()
        if (
            not inventory_types
            or any(value not in AREA_POLICY_INVENTORY_USAGES for value in inventory_types)
            or storage_layout not in {"rack", "pallet_ground", "mixed"}
        ):
            raise WarehouseAreaActivationError(
                f"{area_code} 区发布策略不完整，请返回区域规划补充后重试",
                status_code=409,
            )
        area_name = (
            str(feature.get("formal_area_name") or "").strip()
            or str(feature.get("name") or "").strip()
            or area_code
        )
        if len(area_name) > 100:
            raise WarehouseAreaActivationError(
                f"{area_code} 区名称过长，请返回区域规划缩短后重试",
                status_code=409,
            )
        area = areas_by_code.get(area_code)
        policy = area.storage_policy if area is not None else None
        if has_formal_area_id:
            try:
                expected_area_id = int(raw_formal_area_id)
                expected_floor_id = int(raw_formal_floor_id)
            except (TypeError, ValueError) as error:
                raise WarehouseAreaActivationError(
                    f"{area_code} 区域草稿的正式区域身份无效", status_code=409
                ) from error
            if (
                area is None
                or area.id != expected_area_id
                or area.floor_id != floor.id
                or expected_floor_id != floor.id
            ):
                raise WarehouseAreaActivationError(
                    f"{area_code} 区域草稿对应的正式区域身份已变化", status_code=409
                )
        elif area is not None and policy is None and feature_policy is None:
            raise WarehouseAreaActivationError(
                f"{area_code} 已是现有未绑定区域，必须重新明确选择后发布",
                status_code=409,
            )
        if feature_policy is not None and (
            area is None or feature_policy.area_id != area.id
        ):
            raise WarehouseAreaActivationError(
                f"{area_code} 地图区域已绑定其他正式区域", status_code=409
            )
        if policy is not None and policy.map_feature_id != feature_id:
            raise WarehouseAreaActivationError(
                f"{area_code} 正式区域已绑定其他地图区域", status_code=409
            )
        if feature.get("legacy_v11_name_only") is True:
            requested_name = str(feature.get("formal_area_name") or "").strip()
            if (
                not requested_name
                or area is None
                or policy is not None
                or feature_policy is not None
                or not has_formal_area_id
                or not projected_legacy_binding
                or not legacy_v11_name_only_change_is_safe(
                    db,
                    floor=floor,
                    area=area,
                    feature=feature,
                    feature_area_code_count=feature_area_code_counts.get(
                        area_code, 0
                    ),
                    requested_inventory_types=inventory_types,
                    requested_storage_layout=storage_layout,
                    current_feature=feature,
                )
            ):
                raise WarehouseAreaActivationError(
                    f"{area_code} 区域名称草稿已失去旧版实测区域的唯一身份，"
                    "请刷新后重新保存",
                    status_code=409,
                )
            legacy_name_updates.append(
                (
                    feature,
                    feature_id,
                    area_code,
                    inventory_types,
                    storage_layout,
                    area,
                )
            )
            continue
        if area is None:
            orphaned = db.scalar(
                select(WarehouseLocation.id).where(
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    func.upper(WarehouseLocation.area_code) == area_code,
                ).limit(1)
            )
            if orphaned is not None:
                raise WarehouseAreaActivationError(
                    f"{area_code} 区仍有未纳入正式区域台账的历史库位",
                    status_code=409,
                )
        elif policy is None:
            area_rows = formal_area_location_rows(db, floor=floor, area=area)
            if area_rows and not has_formal_area_id:
                raise WarehouseAreaActivationError(
                    f"{area_code} 区已有正式库位，必须先确认其与实测地图为同一区域",
                    status_code=409,
                )
            transition_blockers = unbound_area_location_transition_blockers(
                db,
                floor=floor,
                area=area,
                requested_inventory_types=inventory_types,
                requested_storage_layout=storage_layout,
            )
            if transition_blockers:
                raise WarehouseAreaActivationError(
                    f"{area_code} 区" + "；".join(transition_blockers),
                    status_code=409,
                )
        else:
            transition_blockers = policy_location_transition_blockers(
                db,
                floor=floor,
                area=area,
                policy=policy,
                requested_inventory_types=inventory_types,
                requested_storage_layout=storage_layout,
            )
            if transition_blockers:
                raise WarehouseAreaActivationError(
                    f"{area_code} 区" + "；".join(transition_blockers),
                    status_code=409,
                )
            route = resolve_area_location_management(
                db,
                floor_code=floor.floor_code,
                area_code=area.area_code,
            )
            desired_type = location_warehouse_type_for_inventory_types(inventory_types)
            if (
                desired_type is not None
                and area.planned_location_count > 0
                and feature_id != defer_location_readiness_for_feature_id
            ):
                active_rows = list(
                    db.scalars(
                        select(WarehouseLocation).where(
                            WarehouseLocation.warehouse_floor == floor.floor_number,
                            func.upper(WarehouseLocation.area_code) == area_code,
                            WarehouseLocation.source_version == route.source_version,
                            WarehouseLocation.is_active.is_(True),
                        )
                    ).all()
                )
                if len(active_rows) != area.planned_location_count or any(
                    row.placement_status != "placed" for row in active_rows
                ):
                    raise WarehouseAreaActivationError(
                        f"{area_code} 区仍有库位未完成布局，不能发布投入使用",
                        status_code=409,
                    )
        proposals.append(
            (feature, feature_id, area_code, inventory_types, storage_layout, area, policy)
        )

    legacy_name_updated_area_codes: list[str] = []
    for (
        feature,
        _feature_id,
        area_code,
        inventory_types,
        storage_layout,
        area,
    ) in legacy_name_updates:
        if not legacy_v11_name_only_change_is_safe(
            db,
            floor=floor,
            area=area,
            feature=feature,
            feature_area_code_count=feature_area_code_counts.get(area_code, 0),
            requested_inventory_types=inventory_types,
            requested_storage_layout=storage_layout,
            current_feature=feature,
        ):
            raise WarehouseAreaActivationError(
                f"{area_code} 区域名称发布前正式身份已变化，请刷新后重试",
                status_code=409,
            )
        old_name = area.area_name
        new_name = str(feature.get("formal_area_name") or "").strip()
        if old_name == new_name:
            continue
        claim = db.execute(
            update(WarehouseArea)
            .where(
                WarehouseArea.id == area.id,
                WarehouseArea.floor_id == floor.id,
                func.upper(WarehouseArea.area_code) == area_code,
                WarehouseArea.area_name == old_name,
                ~select(WarehouseAreaStoragePolicy.id)
                .where(WarehouseAreaStoragePolicy.area_id == area.id)
                .exists(),
            )
            .values(area_name=new_name)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            raise WarehouseAreaActivationError(
                f"{area_code} 区域名称已被其他操作更新，请刷新后重试",
                status_code=409,
            )
        db.expire(area, ["area_name"])
        legacy_name_updated_area_codes.append(area_code)

    published: list[WarehouseAreaStoragePolicy] = []
    for feature, feature_id, area_code, inventory_types, storage_layout, area, policy in proposals:
        area_name = (
            str(feature.get("formal_area_name") or "").strip()
            or str(feature.get("name") or "").strip()
            or area_code
        )
        if area is None:
            area = WarehouseArea(
                floor_id=floor.id,
                area_code=area_code,
                area_name=area_name,
                planned_location_count=0,
                planned_pallet_capacity=0,
                construction_status="layout_building",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(area)
            db.flush()
        desired_types_json = json.dumps(
            inventory_types, ensure_ascii=False, separators=(",", ":")
        )
        if policy is None:
            policy = WarehouseAreaStoragePolicy(
                area_id=area.id,
                map_feature_id=feature_id,
                allowed_inventory_types_json=desired_types_json,
                storage_layout=storage_layout,
                status="draft",
                draft_map_revision=published_revision,
                version=1,
                updated_by=operator_id,
            )
            db.add(policy)
            was_persistent = False
        else:
            was_persistent = True
            expected_policy_version = policy.version
            claim = db.execute(
                update(WarehouseAreaStoragePolicy)
                .where(
                    WarehouseAreaStoragePolicy.id == policy.id,
                    WarehouseAreaStoragePolicy.version == expected_policy_version,
                )
                .values(updated_at=WarehouseAreaStoragePolicy.updated_at)
                .execution_options(synchronize_session=False)
            )
            if claim.rowcount != 1:
                raise WarehouseAreaActivationError(
                    "区域设置已被其他操作更新，请刷新后重试", status_code=409
                )
        if (
            policy.status == "published"
            and policy.published_map_revision == published_revision
            and policy.map_feature_id == feature_id
            and policy_inventory_types(policy) == inventory_types
            and policy.storage_layout == storage_layout
            and area.area_name == area_name
        ):
            continue
        location_type = location_warehouse_type_for_inventory_types(inventory_types)
        area_rows = formal_area_location_rows(db, floor=floor, area=area)
        if location_type is None:
            for row in area_rows:
                row.is_active = False
            area.planned_location_count = 0
        else:
            storage_type = location_storage_type_for_layout(storage_layout)
            for row in area_rows:
                row.warehouse_type = location_type
                row.storage_type = storage_type
        area.area_name = area_name
        policy.map_feature_id = feature_id
        policy.allowed_inventory_types_json = desired_types_json
        policy.storage_layout = storage_layout
        policy.status = "published"
        policy.draft_map_revision = None
        policy.published_map_revision = published_revision
        if was_persistent:
            policy.version += 1
        policy.updated_by = operator_id
        area.construction_status = "enabled"
        published.append(policy)
    db.flush()
    return PublishedAreaPolicyResult(
        published,
        legacy_name_updated_area_codes=legacy_name_updated_area_codes,
    )

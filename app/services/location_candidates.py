from __future__ import annotations

from dataclasses import dataclass
import json

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)


@dataclass(frozen=True)
class OperationalLocationRow:
    location: WarehouseLocation
    floor: WarehouseFloor | None
    area: WarehouseArea | None
    occupied: bool


def _placed_condition():
    return or_(
        WarehouseLocation.placement_status == "placed",
        WarehouseLocation.placement_status.is_(None),
    )


def has_space_ledger(db: Session) -> bool:
    return (
        db.scalar(select(WarehouseFloor.id).order_by(WarehouseFloor.id).limit(1))
        is not None
    )


def _registered_enabled_space_exists():
    return exists(
        select(WarehouseArea.id)
        .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
        .where(
            WarehouseFloor.floor_number == WarehouseLocation.warehouse_floor,
            WarehouseFloor.construction_status == "enabled",
            WarehouseArea.construction_status == "enabled",
            func.upper(WarehouseArea.area_code)
            == func.upper(WarehouseLocation.area_code),
        )
    )


def _live_inventory_exists(location_id_expression):
    return exists(
        select(InventoryLot.id).where(
            InventoryLot.warehouse_location_id == location_id_expression,
            InventoryLot.status.in_(("active", "frozen")),
            (
                InventoryLot.quantity_available
                + InventoryLot.quantity_reserved
                + InventoryLot.quantity_damaged
            )
            > 0,
        )
    )


def location_has_live_inventory(db: Session, location_id: int) -> bool:
    return bool(
        db.scalar(
            select(InventoryLot.id)
            .where(
                InventoryLot.warehouse_location_id == location_id,
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


def operational_location_condition(
    *,
    warehouse_types: set[str] | tuple[str, ...] | None = None,
    pallet_storage_only: bool = False,
):
    conditions = [
        WarehouseLocation.is_active.is_(True),
        _placed_condition(),
        WarehouseLocation.warehouse_floor.is_not(None),
        WarehouseLocation.area_code.is_not(None),
        _registered_enabled_space_exists(),
    ]
    if warehouse_types:
        conditions.append(WarehouseLocation.warehouse_type.in_(tuple(warehouse_types)))
    if pallet_storage_only:
        conditions.append(
            WarehouseLocation.storage_type.in_(("ground", "temporary_aisle"))
        )
    return and_(*conditions)


def operational_location_issue(
    db: Session,
    location: WarehouseLocation,
    *,
    warehouse_types: set[str] | tuple[str, ...] | None = None,
    pallet_storage_only: bool = False,
    require_published: bool = False,
    require_map_geometry: bool = False,
    required_inventory_type: str | None = None,
    require_empty: bool = False,
    capacity_source_location_id: int | None = None,
) -> str | None:
    if not location.is_active:
        return "该库位已停用"
    if require_published and location.placement_status != "placed":
        return "该库位尚未完成正式平面图布局"
    if not require_published and (location.placement_status or "placed") != "placed":
        return "该库位尚未完成平面图布局"
    if warehouse_types and location.warehouse_type not in set(warehouse_types):
        return "所选库位类型与当前业务不匹配"
    if pallet_storage_only and location.storage_type not in {
        "ground",
        "temporary_aisle",
    }:
        return "真实栈板只能选择地面位或过道临放位"

    # Older unit fixtures and pre-ledger databases retain the historical
    # active/placed gate.  Once the space ledger exists, every formal write
    # must also pass its enabled floor and area gates.
    if not has_space_ledger(db):
        if require_published or require_map_geometry:
            return "该库位缺少正式楼层、区域和发布台账"
        return None
    if location.warehouse_floor is None or not (location.area_code or "").strip():
        return "该库位尚未登记楼层和区域"

    floor = db.scalar(
        select(WarehouseFloor).where(
            WarehouseFloor.floor_number == location.warehouse_floor
        )
    )
    if floor is None:
        return "该库位所属楼层尚未建立台账"
    if floor.construction_status != "enabled":
        return "该库位所属楼层尚未启用"
    area = db.scalar(
        select(WarehouseArea).where(
            WarehouseArea.floor_id == floor.id,
            func.upper(WarehouseArea.area_code)
            == str(location.area_code).strip().upper(),
        )
    )
    if area is None:
        return "该库位所属区域尚未建立台账"
    if area.construction_status != "enabled":
        return "该库位所属区域尚未启用"
    if require_published:
        policy = db.scalar(
            select(WarehouseAreaStoragePolicy).where(
                WarehouseAreaStoragePolicy.area_id == area.id
            )
        )
        # V11 is the accepted three-floor map that predates the new area-policy
        # table.  Keep those real mapped slots usable until an area explicitly
        # enters the new draft/published policy lifecycle.  New or draft-bound
        # areas still pass the full policy gate below.
        legacy_v11_map_location = bool(
            policy is None
            and require_map_geometry
            and location.warehouse_floor == 3
            and location.source_version == "V11"
            and location.placement_status == "placed"
        )
        if not legacy_v11_map_location:
            if policy is None or policy.status != "published":
                return "该库位所属区域尚未发布"
            if not (policy.published_map_revision or "").strip():
                return "该库位所属区域缺少已发布地图版本"
            try:
                allowed_types = json.loads(policy.allowed_inventory_types_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                return "该库位所属区域的存放策略已损坏"
            if (
                not isinstance(allowed_types, list)
                or not all(isinstance(value, str) for value in allowed_types)
            ):
                return "该库位所属区域的存放策略已损坏"
            if required_inventory_type and required_inventory_type not in {
                value.strip() for value in allowed_types
            }:
                return "该库位所属区域不允许当前库存类型"
            if (
                pallet_storage_only
                and policy.storage_layout not in {"pallet_ground", "mixed"}
            ):
                return "该库位所属区域的正式存储布局不允许地面栈板"
    if require_map_geometry:
        geometry_id = db.scalar(
            select(Floor3LocationLayout.id)
            .where(Floor3LocationLayout.location_id == location.id)
            .limit(1)
        )
        if geometry_id is None:
            return "该库位缺少已确认的地图几何位置"
    if require_empty:
        occupied_pallet = db.scalar(
            select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id == location.id,
                InventoryPallet.is_current.is_(True),
            )
            .limit(1)
        )
        if occupied_pallet is not None or location_has_live_inventory(db, location.id):
            return "该库位已有活动库存或当前栈板"
    if (
        require_published
        and area.capacity_review_status == "confirmed"
        and area.capacity_eligible
        and area.confirmed_pallet_capacity is not None
    ):
        occupied_count = int(
            db.scalar(
                select(func.count(InventoryPallet.id))
                .join(
                    WarehouseLocation,
                    WarehouseLocation.id == InventoryPallet.location_id,
                )
                .where(
                    InventoryPallet.is_current.is_(True),
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    func.upper(WarehouseLocation.area_code)
                    == str(area.area_code).strip().upper(),
                )
            )
            or 0
        )
        source_in_same_area = False
        if capacity_source_location_id is not None:
            source_location = db.get(WarehouseLocation, capacity_source_location_id)
            source_in_same_area = bool(
                source_location is not None
                and source_location.warehouse_floor == floor.floor_number
                and str(source_location.area_code or "").strip().upper()
                == str(area.area_code).strip().upper()
            )
        if occupied_count >= int(area.confirmed_pallet_capacity) and not source_in_same_area:
            return "该区域已达到现场确认的栈板容量"
    return None


def is_operational_location(
    db: Session,
    location: WarehouseLocation | None,
    *,
    warehouse_types: set[str] | tuple[str, ...] | None = None,
    pallet_storage_only: bool = False,
) -> bool:
    return bool(
        location is not None
        and operational_location_issue(
            db,
            location,
            warehouse_types=warehouse_types,
            pallet_storage_only=pallet_storage_only,
        )
        is None
    )


def list_operational_locations(
    db: Session,
    *,
    warehouse_types: set[str] | tuple[str, ...] | None = None,
    pallet_storage_only: bool = False,
    empty_only: bool = False,
) -> list[OperationalLocationRow]:
    if not has_space_ledger(db):
        query = select(WarehouseLocation).where(
            WarehouseLocation.is_active.is_(True),
            _placed_condition(),
        )
        if warehouse_types:
            query = query.where(
                WarehouseLocation.warehouse_type.in_(tuple(warehouse_types))
            )
        if pallet_storage_only:
            query = query.where(
                WarehouseLocation.storage_type.in_(("ground", "temporary_aisle"))
            )
        locations = db.scalars(
            query.order_by(
                WarehouseLocation.sort_order,
                WarehouseLocation.location_code,
                WarehouseLocation.id,
            )
        ).all()
        rows = [
            OperationalLocationRow(
                location=location,
                floor=None,
                area=None,
                occupied=location_has_live_inventory(db, location.id)
                or bool(
                    db.scalar(
                        select(InventoryPallet.id)
                        .where(
                            InventoryPallet.location_id == location.id,
                            InventoryPallet.is_current.is_(True),
                        )
                        .limit(1)
                    )
                    or db.scalar(
                        select(InventoryLot.id)
                        .where(
                            InventoryLot.warehouse_location_id == location.id,
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
                ),
            )
            for location in locations
        ]
        return [row for row in rows if not empty_only or not row.occupied]

    current_pallet_exists = exists(
        select(InventoryPallet.id).where(
            InventoryPallet.location_id == WarehouseLocation.id,
            InventoryPallet.is_current.is_(True),
        )
    )
    live_inventory_exists = _live_inventory_exists(WarehouseLocation.id)
    occupied_condition = or_(current_pallet_exists, live_inventory_exists)
    query = (
        select(
            WarehouseLocation,
            WarehouseFloor,
            WarehouseArea,
            occupied_condition.label("occupied"),
        )
        .join(
            WarehouseFloor,
            WarehouseFloor.floor_number == WarehouseLocation.warehouse_floor,
        )
        .join(
            WarehouseArea,
            and_(
                WarehouseArea.floor_id == WarehouseFloor.id,
                func.upper(WarehouseArea.area_code)
                == func.upper(WarehouseLocation.area_code),
            ),
        )
        .where(operational_location_condition(
            warehouse_types=warehouse_types,
            pallet_storage_only=pallet_storage_only,
        ))
    )
    if empty_only:
        query = query.where(~occupied_condition)
    rows = db.execute(
        query.order_by(
            WarehouseFloor.floor_number,
            WarehouseArea.area_code,
            WarehouseLocation.sort_order,
            WarehouseLocation.location_code,
            WarehouseLocation.id,
        )
    ).all()
    return [
        OperationalLocationRow(
            location=location,
            floor=floor,
            area=area,
            occupied=bool(occupied),
        )
        for location, floor, area, occupied in rows
    ]


def operational_location_payload(row: OperationalLocationRow) -> dict:
    location = row.location
    floor = row.floor
    area = row.area
    return {
        "id": location.id,
        "location_code": location.location_code,
        "location_name": location.location_name,
        "warehouse_type": location.warehouse_type,
        "warehouse_floor": location.warehouse_floor,
        "floor_id": floor.id if floor else None,
        "floor_code": floor.floor_code if floor else None,
        "floor_name": floor.floor_name if floor else None,
        "area_id": area.id if area else None,
        "area_code": location.area_code,
        "area_name": area.area_name if area else None,
        "storage_type": location.storage_type,
        "is_temporary": bool(location.is_temporary),
        "placement_status": location.placement_status or "placed",
        "sort_order": int(location.sort_order or 0),
        "occupied": row.occupied,
        "is_empty": not row.occupied,
    }

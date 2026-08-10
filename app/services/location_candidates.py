from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
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
) -> str | None:
    if not location.is_active:
        return "该库位已停用"
    if (location.placement_status or "placed") != "placed":
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

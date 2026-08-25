from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
)
from app.services.warehouse_location_address import (
    employee_area_name,
    employee_location_name,
    format_location_address,
    location_address_payload,
    published_measured_map_readiness,
)
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_published_floor_identity,
)


@dataclass(frozen=True)
class OperationalLocationRow:
    location: WarehouseLocation
    floor: WarehouseFloor | None
    area: WarehouseArea | None
    occupied: bool
    projection_context: Mapping[str, object] | None = None


def current_same_location_pallet(
    lot: InventoryLot,
) -> InventoryPallet | None:
    """Return the only pallet that can represent this lot on the map.

    A historical pallet-item link is retained after a pallet is released so a
    delivery cancellation can restore the original handling unit.  Consumers
    must therefore not treat the relationship alone as a current spatial
    projection: the pallet must still be active/current and its location must
    agree with the authoritative lot location.
    """

    pallet_item = getattr(lot, "pallet_item", None)
    pallet = getattr(pallet_item, "pallet", None) if pallet_item is not None else None
    if (
        pallet is None
        or not bool(pallet.is_current)
        or str(pallet.status or "").strip().lower() != "active"
        or pallet.location_id is None
        or lot.warehouse_location_id is None
        or int(pallet.location_id) != int(lot.warehouse_location_id)
    ):
        return None
    return pallet


def pallet_has_physical_goods_condition(pallet_id_expression):
    """SQL predicate matching the dashboard's real product-occupancy rule."""

    return or_(
        exists(
            select(InventoryPalletItem.id).where(
                InventoryPalletItem.pallet_id == pallet_id_expression,
                InventoryPalletItem.inventory_lot_id.is_(None),
                InventoryPalletItem.quantity > 0,
            )
        ),
        exists(
            select(InventoryPalletItem.id)
            .join(
                InventoryLot,
                InventoryLot.id == InventoryPalletItem.inventory_lot_id,
            )
            .where(
                InventoryPalletItem.pallet_id == pallet_id_expression,
                InventoryLot.status.in_(("active", "frozen")),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
        ),
    )


def _map_number(value: object) -> float:
    return round(float(value or 0), 3)


def load_warehouse_location_projection_contexts(
    db: Session,
    locations: Iterable[WarehouseLocation | None],
) -> dict[int, dict[str, object | None]]:
    """Batch-load the only context allowed to classify published map positions."""

    location_rows = [
        row for row in locations if row is not None and row.id is not None
    ]
    location_ids = [int(row.id) for row in location_rows]
    if not location_ids:
        return {}

    space_records = db.execute(
        select(
            WarehouseFloor,
            WarehouseArea,
            WarehouseAreaStoragePolicy,
        )
        .join(WarehouseArea, WarehouseArea.floor_id == WarehouseFloor.id)
        .outerjoin(
            WarehouseAreaStoragePolicy,
            WarehouseAreaStoragePolicy.area_id == WarehouseArea.id,
        )
        .execution_options(populate_existing=True)
    ).all()
    space_by_key = {
        (int(floor.floor_number), str(area.area_code).strip().upper()): {
            "floor": floor,
            "area": area,
            "policy": policy,
        }
        for floor, area, policy in space_records
    }
    layouts_by_location = {
        int(layout.location_id): layout
        for layout in db.scalars(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id.in_(location_ids)
            ).execution_options(populate_existing=True)
        ).all()
    }
    ground_rows_by_location: dict[int, list[dict[str, object]]] = {}
    for plan_id, status, revision, area_id, location_id in db.execute(
        select(
            WarehouseGroundLayoutPlan.id,
            WarehouseGroundLayoutPlan.status,
            WarehouseGroundLayoutPlan.published_map_revision,
            WarehouseGroundLayoutPlan.area_id,
            WarehouseGroundLayoutSlot.location_id,
        )
        .join(
            WarehouseGroundLayoutSlot,
            WarehouseGroundLayoutSlot.plan_id == WarehouseGroundLayoutPlan.id,
        )
        .where(WarehouseGroundLayoutSlot.location_id.in_(location_ids))
    ).all():
        ground_rows_by_location.setdefault(int(location_id), []).append(
            {
                "plan_id": int(plan_id),
                "status": status,
                "published_map_revision": revision,
                "area_id": area_id,
                "location_id": location_id,
            }
        )

    published_identities: dict[int, dict | None] = {}
    for floor_number in {
        int(row.warehouse_floor)
        for row in location_rows
        if row.warehouse_floor is not None
    }:
        try:
            published_identities[floor_number] = (
                load_warehouse_twin_published_floor_identity(floor_number)
            )
        except (OSError, ValueError, WarehouseTwinLayoutNotFoundError):
            published_identities[floor_number] = None

    contexts: dict[int, dict[str, object | None]] = {}
    for location in location_rows:
        floor_number = int(location.warehouse_floor or 0)
        area_code = str(location.area_code or "").strip().upper()
        context = dict(space_by_key.get((floor_number, area_code), {}))
        current_revision = str(
            (published_identities.get(floor_number) or {}).get("revision") or ""
        ).strip()
        ground_candidates = ground_rows_by_location.get(int(location.id), [])
        ground_layout = next(
            (
                candidate
                for candidate in sorted(
                    ground_candidates,
                    key=lambda item: int(item["plan_id"]),
                    reverse=True,
                )
                if candidate["status"] == "published"
                and str(candidate["published_map_revision"] or "").strip()
                == current_revision
            ),
            (
                max(
                    ground_candidates,
                    key=lambda item: int(item["plan_id"]),
                )
                if ground_candidates
                else None
            ),
        )
        context.update(
            {
                "layout": layouts_by_location.get(int(location.id)),
                "published_floor_identity": published_identities.get(floor_number),
                "ground_layout": ground_layout,
            }
        )
        contexts[int(location.id)] = context
    return contexts


def warehouse_location_projection(
    location: WarehouseLocation,
    *,
    floor: WarehouseFloor | None = None,
    area: WarehouseArea | None = None,
    policy: WarehouseAreaStoragePolicy | Mapping[str, object] | None = None,
    published_floor_identity: Mapping[str, object] | None = None,
    ground_layout: Mapping[str, object] | None = None,
    layout: Floor3LocationLayout | None = None,
) -> dict[str, object | None]:
    """Return the canonical current published-map projection for one location."""

    readiness = published_measured_map_readiness(
        location,
        floor=floor,
        area=area,
        policy=policy,
        published_floor_identity=published_floor_identity,
        has_geometry=layout is not None,
        ground_layout=ground_layout,
    )
    map_position = None
    if readiness.position_status == "mapped" and layout is not None:
        map_position = {
            "left_pct": _map_number(layout.left_pct),
            "top_pct": _map_number(layout.top_pct),
            "width_pct": _map_number(layout.width_pct),
            "height_pct": _map_number(layout.height_pct),
            "z_index": int(layout.z_index or 0),
            "version": int(layout.version),
            "source_type": layout.source_type,
            "layout_kind": layout.layout_kind,
            "map_feature_id": readiness.map_feature_id,
            "published_map_revision": readiness.published_map_revision,
        }
    source_version = str(location.source_version or "").strip().upper()
    if readiness.position_status == "mapped":
        map_status = "floor3_mapped" if source_version == "V11" else "twin_mapped"
    elif source_version in {"V11", "TWIN_V1"}:
        map_status = "unplaced"
    else:
        map_status = "ledger_only"
    return {
        "position_status": readiness.position_status,
        "map_status": map_status,
        "map_position": map_position,
        "map_feature_id": readiness.map_feature_id,
        "published_map_revision": readiness.published_map_revision,
        "map_issue": readiness.issue,
    }


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


def claim_warehouse_floor_projection(
    db: Session,
    *,
    floor_number: int,
) -> bool:
    """Acquire the persistent floor mutex shared by map and inventory writes.

    The no-op update is held until the caller commits or rolls back.  SQLite
    therefore serializes every writer, while row-locking databases serialize
    projection changes and inventory placement on the same floor row.
    """

    result = db.execute(
        update(WarehouseFloor)
        .where(WarehouseFloor.floor_number == int(floor_number))
        .values(
            construction_status=WarehouseFloor.construction_status,
            updated_at=WarehouseFloor.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _current_pallet_exists(location_id_expression):
    return exists(
        select(InventoryPallet.id).where(
            InventoryPallet.location_id == location_id_expression,
            InventoryPallet.is_current.is_(True),
        )
    )


def _active_ground_occupancy_exists(location_id_expression):
    return exists(
        select(WarehouseGroundOccupancySlot.id)
        .join(
            WarehouseGroundOccupancy,
            WarehouseGroundOccupancy.id
            == WarehouseGroundOccupancySlot.occupancy_id,
        )
        .where(
            WarehouseGroundOccupancySlot.location_id == location_id_expression,
            WarehouseGroundOccupancySlot.status == "active",
            WarehouseGroundOccupancy.status == "active",
        )
    )


def claim_active_placed_location(
    db: Session,
    location_id: int,
    *,
    expected_layout_version: int | None = None,
) -> bool:
    """Serialize a destination write with layout/state changes on one location.

    The guarded no-op is deliberately the first physical write in inventory
    workflows.  SQLite obtains its writer lock here; databases with row-level
    locking serialize on the same warehouse-location row.
    """

    floor_number = db.scalar(
        select(WarehouseLocation.warehouse_floor).where(
            WarehouseLocation.id == location_id
        )
    )
    if floor_number is not None:
        floor_exists = db.scalar(
            select(WarehouseFloor.id).where(
                WarehouseFloor.floor_number == int(floor_number)
            )
        )
        if floor_exists is not None and not claim_warehouse_floor_projection(
            db,
            floor_number=int(floor_number),
        ):
            return False
        if floor_exists is not None:
            # Refresh only the claimed target.  ``expire_all`` would discard
            # unrelated unflushed business changes in composite receipt,
            # stocktake and return transactions.
            db.get(
                WarehouseLocation,
                int(location_id),
                populate_existing=True,
            )

    conditions = [
        WarehouseLocation.id == location_id,
        WarehouseLocation.is_active.is_(True),
        _placed_condition(),
    ]
    layout_exists = exists(
        select(Floor3LocationLayout.id).where(
            Floor3LocationLayout.location_id == WarehouseLocation.id,
        )
    )
    if expected_layout_version is not None:
        conditions.append(
            exists(
                select(Floor3LocationLayout.id).where(
                    Floor3LocationLayout.location_id == WarehouseLocation.id,
                    Floor3LocationLayout.version == expected_layout_version,
                )
            )
        )
        # A two-slot large occupancy has no pallet or InventoryLot on its
        # secondary location. It must still block every ordinary writer. The
        # primary location may accept an explicitly validated same-product
        # co-location because its current pallet proves the authoritative
        # container identity.
        conditions.append(
            or_(
                ~_active_ground_occupancy_exists(WarehouseLocation.id),
                _current_pallet_exists(WarehouseLocation.id),
            )
        )
    else:
        # A mapped empty slot is allowed to move automatically.  Any workflow
        # that would make it occupied must therefore prove which map version
        # the operator selected. Existing occupied slots are already fixed and
        # may accept same-location quantity changes without a map token.
        conditions.append(
            or_(
                ~layout_exists,
                _live_inventory_exists(WarehouseLocation.id),
                _current_pallet_exists(WarehouseLocation.id),
            )
        )
    result = db.execute(
        update(WarehouseLocation)
        .where(*conditions)
        .values(
            is_active=WarehouseLocation.is_active,
            updated_at=WarehouseLocation.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


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
    projection_context: Mapping[str, object] | None = None,
    known_occupied: bool | None = None,
    area_occupied_pallet_count: int | None = None,
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
    if projection_context is None and not has_space_ledger(db):
        if require_published or require_map_geometry:
            return "该库位缺少正式楼层、区域和发布台账"
        return None
    if location.warehouse_floor is None or not (location.area_code or "").strip():
        return "该库位尚未登记楼层和区域"

    context = dict(
        projection_context
        if projection_context is not None
        else load_warehouse_location_projection_contexts(db, [location]).get(
            int(location.id), {}
        )
    )
    floor = context.get("floor")
    if floor is None:
        return "该库位所属楼层尚未建立台账"
    assert isinstance(floor, WarehouseFloor)
    if floor.construction_status != "enabled":
        return "该库位所属楼层尚未启用"
    area = context.get("area")
    if area is None:
        return "该库位所属区域尚未建立台账"
    assert isinstance(area, WarehouseArea)
    if area.construction_status != "enabled":
        return "该库位所属区域尚未启用"
    policy = context.get("policy")
    if require_published:
        projection = warehouse_location_projection(
            location,
            **context,
        )
        if projection["position_status"] != "mapped":
            return str(projection["map_issue"] or "该库位尚未发布到当前实测地图")
        if policy is not None:
            assert isinstance(policy, WarehouseAreaStoragePolicy)
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
    elif require_map_geometry:
        if context.get("layout") is None:
            return "该库位缺少已确认的地图几何位置"
    if require_empty:
        occupied = known_occupied
        if occupied is None:
            occupied_pallet = db.scalar(
                select(InventoryPallet.id)
                .where(
                    InventoryPallet.location_id == location.id,
                    InventoryPallet.is_current.is_(True),
                )
                .limit(1)
            )
            occupied_ground_slot = db.scalar(
                select(WarehouseGroundOccupancySlot.id)
                .join(WarehouseGroundOccupancy)
                .where(
                    WarehouseGroundOccupancySlot.location_id == location.id,
                    WarehouseGroundOccupancySlot.status == "active",
                    WarehouseGroundOccupancy.status == "active",
                )
                .limit(1)
            )
            occupied = bool(
                occupied_pallet is not None
                or occupied_ground_slot is not None
                or location_has_live_inventory(db, location.id)
            )
        if occupied:
            return "该库位已有活动库存或当前栈板"
    if (
        require_published
        and area.capacity_review_status == "confirmed"
        and area.capacity_eligible
        and area.confirmed_pallet_capacity is not None
    ):
        occupied_count = area_occupied_pallet_count
        if occupied_count is None:
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
    occupied_condition = or_(
        current_pallet_exists,
        live_inventory_exists,
        _active_ground_occupancy_exists(WarehouseLocation.id),
    )
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
    projection_contexts = load_warehouse_location_projection_contexts(
        db, [location for location, _floor, _area, _occupied in rows]
    )
    return [
        OperationalLocationRow(
            location=location,
            floor=floor,
            area=area,
            occupied=bool(occupied),
            projection_context=projection_contexts.get(int(location.id)),
        )
        for location, floor, area, occupied in rows
    ]


def operational_location_payload(row: OperationalLocationRow) -> dict:
    location = row.location
    floor = row.floor
    area = row.area
    context = dict(row.projection_context or {})
    if floor is not None:
        context["floor"] = floor
    if area is not None:
        context["area"] = area
    projection = warehouse_location_projection(location, **context)
    address_area = area or getattr(location, "address_area", None)
    current_address_code, current_address_name = format_location_address(
        location,
        area=address_area,
        floor=floor,
    )
    address_payload = location_address_payload(
        location,
        area=address_area,
        floor=floor,
        position_status=str(projection["position_status"]),
    )
    employee_name = employee_location_name(
        location,
        area=address_area,
        floor=floor,
    )
    layout = context.get("layout")
    return {
        "id": location.id,
        "location_code": location.location_code,
        "location_name": employee_name,
        "location_master_name": location.location_name,
        "warehouse_type": location.warehouse_type,
        "warehouse_floor": location.warehouse_floor,
        "floor_id": floor.id if floor else None,
        "floor_code": floor.floor_code if floor else None,
        "floor_name": floor.floor_name if floor else None,
        "area_id": area.id if area else None,
        "area_code": location.area_code,
        "area_name": employee_area_name(
            area,
            area_code=location.area_code,
            floor_number=(floor.floor_number if floor is not None else location.warehouse_floor),
        ),
        "area_master_name": area.area_name if area else None,
        "storage_type": location.storage_type,
        "is_temporary": bool(location.is_temporary),
        "placement_status": location.placement_status or "unplaced",
        "is_active": bool(location.is_active),
        "layout_version": (
            int(layout.version)
            if isinstance(layout, Floor3LocationLayout)
            else None
        ),
        **projection,
        "sort_order": int(location.sort_order or 0),
        "occupied": row.occupied,
        "is_empty": not row.occupied,
        "address_kind": location.address_kind,
        "address_area_id": location.address_area_id,
        "address_zone_code": (
            address_area.address_zone_code if address_area is not None else None
        ),
        "address_subzone_no": (
            address_area.address_subzone_no if address_area is not None else None
        ),
        "rack_code": location.rack_code,
        "ground_row_no": location.ground_row_no,
        "level_no": location.level_no,
        "slot_no": location.slot_no,
        "address_version": int(location.address_version or 1),
        "current_address_code": current_address_code,
        "current_address_name": current_address_name,
        "employee_location_name": employee_name,
        "projection_source": address_payload["projection_source"],
    }

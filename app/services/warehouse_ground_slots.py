from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from math import isclose

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseLocation,
)
from app.core.time_contract import utc_now_naive
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError,
    measured_pallet_slots_for_zone,
)
from app.services.warehouse_area_activation import (
    WarehouseAreaActivationError,
    policy_inventory_types,
)
from app.services.warehouse_location_address import employee_location_name


GROUND_LAYOUT_SOURCE_VERSION = "P1-87"
GROUND_LOCATION_SOURCE_VERSION = "CURRENT_MAP"
NUMBERING_ORIGINS = {"south", "north", "west", "east"}
ROW_DIRECTIONS = {"from_aisle_inward", "from_inside_outward"}
SLOT_DIRECTIONS = {"left_to_right", "right_to_left"}


class WarehouseGroundSlotError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _floor_name(floor_number: int) -> str:
    return {1: "一楼", 2: "二楼", 3: "三楼", 4: "四楼"}.get(
        floor_number, f"{floor_number}楼"
    )


def _ordered_physical_slots(
    slots: list[dict],
    *,
    numbering_origin: str,
    row_direction: str,
    slot_direction: str,
) -> list[list[dict]]:
    if numbering_origin not in NUMBERING_ORIGINS:
        raise WarehouseGroundSlotError(
            "GROUND_NUMBERING_ORIGIN_INVALID", "编号起点必须选择南、北、西或东侧主通道。", status_code=422
        )
    if row_direction not in ROW_DIRECTIONS:
        raise WarehouseGroundSlotError(
            "GROUND_ROW_DIRECTION_INVALID", "排方向无效。", status_code=422
        )
    if slot_direction not in SLOT_DIRECTIONS:
        raise WarehouseGroundSlotError(
            "GROUND_SLOT_DIRECTION_INVALID", "位方向无效。", status_code=422
        )

    horizontal_rows = numbering_origin in {"south", "north"}
    groups: dict[float, list[dict]] = defaultdict(list)
    for slot in slots:
        center = (
            float(slot["y_mm"]) + float(slot["depth_mm"]) / 2
            if horizontal_rows
            else float(slot["x_mm"]) + float(slot["width_mm"]) / 2
        )
        groups[round(center, 3)].append(slot)

    natural_ascending = numbering_origin in {"south", "west"}
    if row_direction == "from_inside_outward":
        natural_ascending = not natural_ascending
    row_keys = sorted(groups, reverse=not natural_ascending)

    rows: list[list[dict]] = []
    for key in row_keys:
        current = groups[key]
        if horizontal_rows:
            left_to_right_ascending = numbering_origin == "south"
            coordinate = lambda row: float(row["x_mm"]) + float(row["width_mm"]) / 2
        else:
            # Facing east from the west aisle, the operator's left side is the
            # map's higher Y side. Facing west reverses that relationship.
            left_to_right_ascending = numbering_origin == "east"
            coordinate = lambda row: float(row["y_mm"]) + float(row["depth_mm"]) / 2
        if slot_direction == "right_to_left":
            left_to_right_ascending = not left_to_right_ascending
        rows.append(sorted(current, key=coordinate, reverse=not left_to_right_ascending))
    return rows


def number_ground_physical_slots(
    slots: list[dict],
    *,
    numbering_origin: str,
    row_direction: str,
    slot_direction: str,
    row_start_no: int = 1,
    slot_start_no: int = 1,
) -> list[dict]:
    """Attach deterministic route, row and slot numbers to measured footprints."""

    if not 1 <= row_start_no <= 99 or not 1 <= slot_start_no <= 99:
        raise WarehouseGroundSlotError(
            "GROUND_NUMBER_START_INVALID",
            "排号和位号起点必须在 1～99 之间。",
            status_code=422,
        )
    numbered: list[dict] = []
    route_sequence = 0
    for row_index, row in enumerate(
        _ordered_physical_slots(
            slots,
            numbering_origin=numbering_origin,
            row_direction=row_direction,
            slot_direction=slot_direction,
        )
    ):
        row_no = row_start_no + row_index
        if row_no > 99:
            raise WarehouseGroundSlotError(
                "GROUND_ROW_NUMBER_OVERFLOW",
                "生成后的排号超过 99，请调整编号起点。",
            )
        for slot_index, slot in enumerate(row):
            slot_no = slot_start_no + slot_index
            if slot_no > 99:
                raise WarehouseGroundSlotError(
                    "GROUND_SLOT_NUMBER_OVERFLOW",
                    "生成后的位号超过 99，请调整编号起点。",
                )
            route_sequence += 1
            numbered.append(
                {
                    **slot,
                    "route_sequence": route_sequence,
                    "row_no": row_no,
                    "slot_no": slot_no,
                }
            )
    return numbered


def build_ground_slot_preview(
    floor_layout: dict,
    *,
    feature_id: str,
    floor_number: int,
    zone_code: str,
    subzone_no: int,
    target_slot_count: int,
    numbering_origin: str,
    row_direction: str,
    slot_direction: str,
    row_start_no: int,
    slot_start_no: int,
) -> list[dict]:
    """Build deterministic real-pallet footprints without logical-anchor fallback."""

    if not 1 <= target_slot_count <= 500:
        raise WarehouseGroundSlotError(
            "GROUND_SLOT_COUNT_INVALID", "地堆位置数量必须在 1～500 之间。", status_code=422
        )
    if not 1 <= row_start_no <= 99 or not 1 <= slot_start_no <= 99:
        raise WarehouseGroundSlotError(
            "GROUND_NUMBER_START_INVALID", "排号和位号起点必须在 1～99 之间。", status_code=422
        )
    try:
        physical = measured_pallet_slots_for_zone(floor_layout, feature_id=feature_id)
    except Floor1CandidatePlanningError as error:
        raise WarehouseGroundSlotError(
            "GROUND_MAP_GEOMETRY_INVALID", str(error), status_code=error.status_code
        ) from error
    if len(physical) < target_slot_count:
        raise WarehouseGroundSlotError(
            "GROUND_STANDARD_SLOTS_INSUFFICIENT",
            f"实测区域仅能安全放置 {len(physical)} 个 1200×1000mm 标准栈板位，不能生成 {target_slot_count} 个。",
        )

    ordered_rows = _ordered_physical_slots(
        physical,
        numbering_origin=numbering_origin,
        row_direction=row_direction,
        slot_direction=slot_direction,
    )
    selected: list[dict] = []
    route_sequence = 0
    for row_index, row in enumerate(ordered_rows):
        row_no = row_start_no + row_index
        if row_no > 99:
            raise WarehouseGroundSlotError(
                "GROUND_ROW_NUMBER_OVERFLOW", "生成后的排号超过 99，请调整编号起点。"
            )
        for slot_index, slot in enumerate(row):
            if len(selected) >= target_slot_count:
                break
            slot_no = slot_start_no + slot_index
            if slot_no > 99:
                raise WarehouseGroundSlotError(
                    "GROUND_SLOT_NUMBER_OVERFLOW", "生成后的位号超过 99，请调整编号起点。"
                )
            route_sequence += 1
            area_code = f"{zone_code.upper()}{subzone_no:02d}"
            selected.append(
                {
                    **slot,
                    "route_sequence": route_sequence,
                    "row_no": row_no,
                    "slot_no": slot_no,
                    "location_code": f"{floor_number}F-{area_code}-P{row_no:02d}-{slot_no:02d}",
                    "location_name": (
                        f"{_floor_name(floor_number)} {zone_code.upper()}{subzone_no}区·"
                        f"第{row_no}排·{slot_no}号位"
                    ),
                    "layout_kind": "physical_pallet",
                }
            )
        if len(selected) >= target_slot_count:
            break
    return selected


def ground_preview_fingerprint(
    *,
    area_id: int,
    policy_version: int,
    map_revision: str,
    configuration: dict,
    slots: list[dict],
) -> str:
    return canonical_hash(
        {
            "rule_version": GROUND_LAYOUT_SOURCE_VERSION,
            "area_id": area_id,
            "policy_version": policy_version,
            "map_revision": map_revision,
            "configuration": configuration,
            "slots": [
                {
                    **{
                        key: row[key]
                        for key in (
                            "route_sequence",
                            "row_no",
                            "slot_no",
                            "location_code",
                            "x_mm",
                            "y_mm",
                            "width_mm",
                            "depth_mm",
                            "left_pct",
                            "top_pct",
                            "width_pct",
                            "height_pct",
                        )
                    },
                    **(
                        {"existing_location_id": row["existing_location_id"]}
                        if "existing_location_id" in row
                        else {}
                    ),
                    **(
                        {"existing_layout_version": row["existing_layout_version"]}
                        if "existing_layout_version" in row
                        else {}
                    ),
                }
                for row in slots
            ],
        }
    )


def active_ground_occupancy_for_location(
    db: Session, location_id: int
) -> WarehouseGroundOccupancy | None:
    return db.scalar(
        select(WarehouseGroundOccupancy)
        .join(WarehouseGroundOccupancySlot)
        .where(
            WarehouseGroundOccupancySlot.location_id == location_id,
            WarehouseGroundOccupancySlot.status == "active",
            WarehouseGroundOccupancy.status == "active",
        )
        .options(
            selectinload(WarehouseGroundOccupancy.slots),
            selectinload(WarehouseGroundOccupancy.pallet)
            .selectinload(InventoryPallet.items)
            .selectinload(InventoryPalletItem.inventory_lot),
        )
    )


def active_ground_occupancy_for_pallet(
    db: Session,
    pallet_id: int,
) -> WarehouseGroundOccupancy | None:
    return db.scalar(
        select(WarehouseGroundOccupancy)
        .where(
            WarehouseGroundOccupancy.pallet_id == pallet_id,
            WarehouseGroundOccupancy.status == "active",
        )
        .options(selectinload(WarehouseGroundOccupancy.slots))
    )


def release_ground_occupancy_for_pallet(
    db: Session,
    *,
    pallet_id: int,
    operator_id: int | None,
) -> WarehouseGroundOccupancy | None:
    occupancy = active_ground_occupancy_for_pallet(db, pallet_id)
    if occupancy is None:
        return None
    now = utc_now_naive()
    occupancy.status = "released"
    occupancy.version = int(occupancy.version or 1) + 1
    occupancy.released_by = operator_id
    occupancy.released_at = now
    for slot in occupancy.slots:
        if slot.status == "active":
            slot.status = "released"
            slot.released_at = now
    db.flush()
    return occupancy


def restore_ground_occupancy_for_pallet(
    db: Session,
    *,
    pallet_id: int,
    location_id: int,
) -> WarehouseGroundOccupancy | None:
    occupancy = db.scalar(
        select(WarehouseGroundOccupancy)
        .where(
            WarehouseGroundOccupancy.pallet_id == pallet_id,
            WarehouseGroundOccupancy.primary_location_id == location_id,
            WarehouseGroundOccupancy.status == "released",
        )
        .order_by(WarehouseGroundOccupancy.id.desc())
        .options(selectinload(WarehouseGroundOccupancy.slots))
    )
    if occupancy is None:
        return None
    slot_ids = [slot.location_id for slot in occupancy.slots]
    conflict = db.scalar(
        select(WarehouseGroundOccupancySlot.id)
        .join(WarehouseGroundOccupancy)
        .where(
            WarehouseGroundOccupancySlot.location_id.in_(slot_ids),
            WarehouseGroundOccupancySlot.status == "active",
            WarehouseGroundOccupancy.status == "active",
            WarehouseGroundOccupancy.id != occupancy.id,
        )
        .limit(1)
    )
    if conflict is not None:
        raise WarehouseGroundSlotError(
            "GROUND_RESTORE_TARGET_OCCUPIED",
            "原地堆位置已被其他货物占用，无法恢复送货前空间占用。",
        )
    occupancy.status = "active"
    occupancy.version = int(occupancy.version or 1) + 1
    occupancy.released_by = None
    occupancy.released_at = None
    for slot in occupancy.slots:
        slot.status = "active"
        slot.released_at = None
    db.flush()
    return occupancy


def occupancy_physical_quantity(occupancy: WarehouseGroundOccupancy) -> int:
    total = 0
    for item in occupancy.pallet.items:
        lot = item.inventory_lot
        if lot is None or lot.status not in {"active", "frozen"}:
            continue
        total += max(
            int(lot.quantity_available or 0)
            + int(lot.quantity_reserved or 0)
            + int(lot.quantity_damaged or 0),
            0,
        )
    return total


def ground_slots_adjacent(left: WarehouseGroundLayoutSlot, right: WarehouseGroundLayoutSlot) -> bool:
    if left.plan_id != right.plan_id or left.location_id == right.location_id:
        return False
    lx, ly = float(left.x_mm), float(left.y_mm)
    rx, ry = float(right.x_mm), float(right.y_mm)
    lw, ld = float(left.width_mm), float(left.depth_mm)
    rw, rd = float(right.width_mm), float(right.depth_mm)
    vertical_edge = (isclose(lx + lw, rx, abs_tol=1.0) or isclose(rx + rw, lx, abs_tol=1.0)) and (
        min(ly + ld, ry + rd) - max(ly, ry) > 1.0
    )
    horizontal_edge = (isclose(ly + ld, ry, abs_tol=1.0) or isclose(ry + rd, ly, abs_tol=1.0)) and (
        min(lx + lw, rx + rw) - max(lx, rx) > 1.0
    )
    return vertical_edge or horizontal_edge


def published_ground_plan(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
    required_inventory_type: str | None = None,
) -> WarehouseGroundLayoutPlan:
    plan = db.scalar(
        select(WarehouseGroundLayoutPlan)
        .join(WarehouseArea, WarehouseArea.id == WarehouseGroundLayoutPlan.area_id)
        .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
        .where(
            func.upper(WarehouseFloor.floor_code) == floor_code.strip().upper(),
            func.upper(WarehouseArea.area_code) == area_code.strip().upper(),
            WarehouseGroundLayoutPlan.status == "published",
        )
        .options(
            selectinload(WarehouseGroundLayoutPlan.area)
            .selectinload(WarehouseArea.floor),
            selectinload(WarehouseGroundLayoutPlan.area)
            .selectinload(WarehouseArea.storage_policy),
            selectinload(WarehouseGroundLayoutPlan.slots)
            .selectinload(WarehouseGroundLayoutSlot.location)
            .selectinload(WarehouseLocation.floor3_layout),
        )
    )
    if plan is None:
        raise WarehouseGroundSlotError(
            "GROUND_LAYOUT_NOT_PUBLISHED", "当前区域尚未发布地堆排位。", status_code=404
        )
    policy = plan.area.storage_policy
    current_map_applied = False
    if policy and policy.published_map_revision != plan.published_map_revision:
        from app.services.warehouse_ground_map_application import load_map_applications, application_matches
        from app.services.warehouse_twin_layout import load_warehouse_twin_published_floor_identity
        identity = load_warehouse_twin_published_floor_identity(plan.area.floor.floor_number)
        current_revision = str((identity or {}).get("revision") or "")
        receipt = load_map_applications(db, [plan.id]).get(plan.id)
        active = [slot for slot in plan.slots if slot.location.is_active]
        current_map_applied = bool(active) and current_revision == policy.published_map_revision and all(
            application_matches(receipt, plan_id=plan.id, plan_version=plan.version,
                area_id=plan.area_id, policy=policy, revision=current_revision,
                location=slot.location, layout=slot.location.floor3_layout) for slot in active)
    if (
        policy is None
        or policy.status != "published"
        or policy.storage_layout not in {"pallet_ground", "mixed"}
        or (policy.published_map_revision != plan.published_map_revision and not current_map_applied)
    ):
        raise WarehouseGroundSlotError(
            "GROUND_LAYOUT_PUBLISH_STALE", "区域地图或存放策略已变化，请管理员重新核对地堆排位。"
        )
    try:
        allowed_inventory_types = set(policy_inventory_types(policy))
    except WarehouseAreaActivationError as error:
        raise WarehouseGroundSlotError(
            "GROUND_AREA_POLICY_INVALID",
            str(error),
            status_code=error.status_code,
        ) from error
    if required_inventory_type and required_inventory_type not in allowed_inventory_types:
        raise WarehouseGroundSlotError(
            "GROUND_AREA_INVENTORY_TYPE_NOT_ALLOWED",
            "该区域的正式用途不允许存放本次库存，不能作为可选地堆位置。",
        )
    return plan


def ground_candidate_rows(
    db: Session,
    *,
    plan: WarehouseGroundLayoutPlan,
    customer_id: int,
    product_id: int,
    incoming_quantity: int,
    can_view_occupied_details: bool,
) -> list[dict]:
    rows: list[dict] = []
    location_ids = [slot.location_id for slot in plan.slots]
    area_sequence_by_location = {
        int(location_id): sequence
        for sequence, location_id in enumerate(
            db.scalars(
                select(WarehouseLocation.id)
                .where(
                    WarehouseLocation.warehouse_floor
                    == int(plan.area.floor.floor_number),
                    func.upper(func.trim(WarehouseLocation.area_code))
                    == str(plan.area.area_code or "").strip().upper(),
                )
                .order_by(
                    WarehouseLocation.sort_order,
                    WarehouseLocation.location_code,
                    WarehouseLocation.id,
                )
            ).all(),
            start=1,
        )
    }
    occupancies = list(
        db.scalars(
            select(WarehouseGroundOccupancy)
            .join(WarehouseGroundOccupancySlot)
            .where(
                WarehouseGroundOccupancySlot.location_id.in_(location_ids),
                WarehouseGroundOccupancySlot.status == "active",
                WarehouseGroundOccupancy.status == "active",
            )
            .options(
                selectinload(WarehouseGroundOccupancy.slots),
                selectinload(WarehouseGroundOccupancy.pallet)
                .selectinload(InventoryPallet.items)
                .selectinload(InventoryPalletItem.inventory_lot),
            )
        ).unique().all()
    )
    occupancy_by_location_id = {
        row.location_id: occupancy
        for occupancy in occupancies
        for row in occupancy.slots
        if row.status == "active"
    }
    current_pallet_location_ids = set(
        db.scalars(
            select(InventoryPallet.location_id).where(
                InventoryPallet.location_id.in_(location_ids),
                InventoryPallet.is_current.is_(True),
            )
        ).all()
    )
    physical_quantity = (
        InventoryLot.quantity_available
        + InventoryLot.quantity_reserved
        + InventoryLot.quantity_damaged
    )
    live_lot_location_ids = set(
        db.scalars(
            select(InventoryLot.warehouse_location_id).where(
                InventoryLot.warehouse_location_id.in_(location_ids),
                InventoryLot.status.in_(("active", "frozen")),
                physical_quantity > 0,
            )
        ).all()
    )

    def local_issue(location: WarehouseLocation) -> str | None:
        if not location.is_active:
            return "该库位已停用"
        if location.placement_status != "placed" or location.floor3_layout is None:
            return "该库位缺少已确认的地图几何位置"
        if location.warehouse_type not in {"finished", "shared"}:
            return "所选库位类型与成品业务不匹配"
        if location.storage_type not in {"ground", "temporary_aisle"}:
            return "该位置不是地面栈板位"
        return None

    for slot in plan.slots:
        location = slot.location
        layout = location.floor3_layout
        issue = local_issue(location)
        occupancy = occupancy_by_location_id.get(location.id)
        current_quantity = occupancy_physical_quantity(occupancy) if occupancy else 0
        status = "empty"
        color = "green"
        selectable = issue is None and occupancy is None
        reason = "空位，可存放"
        current_product = None
        remaining_capacity = None
        if issue:
            status, color, selectable, reason = "unavailable", "gray", False, issue
        elif occupancy is not None:
            is_primary = occupancy.primary_location_id == location.id
            same_product = (
                occupancy.customer_id == customer_id and occupancy.product_id == product_id
            )
            remaining_capacity = max(int(occupancy.capacity_quantity) - current_quantity, 0)
            if is_primary and same_product and incoming_quantity <= remaining_capacity:
                status, color, selectable = "same_product", "blue", True
                reason = "同客户同存货编码且容量足够，可由操作员选择共位"
                current_product = {
                    "customer_id": occupancy.customer_id,
                    "product_id": occupancy.product_id,
                }
            elif is_primary and same_product:
                status, color, selectable = "capacity_full", "gray", False
                reason = "同款位置剩余容量不足"
                if can_view_occupied_details:
                    current_product = {
                        "customer_id": occupancy.customer_id,
                        "product_id": occupancy.product_id,
                    }
            else:
                status, color, selectable = "conflict", "red", False
                reason = "位置已被其他客户、其他存货或大型货物占用"
        elif location.id in current_pallet_location_ids or location.id in live_lot_location_ids:
            status, color, selectable = "conflict", "red", False
            reason = "位置有尚未纳入地堆占用关系的库存，请管理员先核对"

        adjacent_ids = []
        if status == "empty":
            for other in plan.slots:
                if ground_slots_adjacent(slot, other):
                    other_occupancy = occupancy_by_location_id.get(other.location_id)
                    other_has_inventory = (
                        other.location_id in current_pallet_location_ids
                        or other.location_id in live_lot_location_ids
                    )
                    if (
                        other_occupancy is None
                        and not other_has_inventory
                        and local_issue(other.location) is None
                    ):
                        adjacent_ids.append(other.location_id)
        rows.append(
            {
                "location_id": location.id,
                "location_name": employee_location_name(
                    location,
                    area=plan.area,
                    floor=plan.area.floor,
                    area_sequence=area_sequence_by_location.get(int(location.id)),
                ),
                "location_master_name": location.location_name,
                "row_no": slot.row_no,
                "slot_no": slot.slot_no,
                "route_sequence": slot.route_sequence,
                "layout_version": int(layout.version) if layout is not None else None,
                "status": status,
                "color": color,
                "selectable": selectable,
                "reason": reason,
                "current_quantity": current_quantity,
                "capacity_quantity": int(occupancy.capacity_quantity) if occupancy else None,
                "remaining_capacity": remaining_capacity,
                "current_product": current_product,
                "adjacent_location_ids": sorted(adjacent_ids),
                "geometry": {
                    "left_pct": layout.left_pct if layout is not None else None,
                    "top_pct": layout.top_pct if layout is not None else None,
                    "width_pct": layout.width_pct if layout is not None else None,
                    "height_pct": layout.height_pct if layout is not None else None,
                },
            }
        )
    return rows


def ground_occupancy_payload(occupancy: WarehouseGroundOccupancy) -> dict:
    return {
        "occupancy_id": occupancy.id,
        "primary_location_id": occupancy.primary_location_id,
        "footprint_kind": occupancy.footprint_kind,
        "slot_count": len([row for row in occupancy.slots if row.status == "active"]),
        "location_ids": [
            row.location_id
            for row in sorted(occupancy.slots, key=lambda item: item.slot_sequence)
            if row.status == "active"
        ],
        "capacity_quantity": occupancy.capacity_quantity,
        "current_quantity": occupancy_physical_quantity(occupancy),
        "version": occupancy.version,
    }

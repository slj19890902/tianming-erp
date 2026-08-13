from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re

from sqlalchemy import func, select
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
from app.services.warehouse_area_activation import AREA_LOCATION_SOURCE_VERSION


STANDARD_PALLET_WIDTH_MM = 1200
STANDARD_PALLET_DEPTH_MM = 1000
_USAGE_BY_SUBTYPE = {
    "finished_wait_delivery": "finished",
    "semi_finished": "semi_finished",
    "raw_material": "raw_material",
    "mold": "mold",
    "printing_plate": "print_plate",
    "temporary_turnover": "temporary_turnover",
}
_INVENTORY_LOCATION_USAGES = frozenset({"finished", "semi_finished"})
_ASSET_USAGES = frozenset({"mold", "print_plate"})


class Floor1CandidatePlanningError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Floor1CandidateApplyResult:
    plan: dict
    applied: bool
    areas: tuple[WarehouseArea, ...]
    locations: tuple[WarehouseLocation, ...]
    archived_legacy_areas: tuple[WarehouseArea, ...]


def _area_code(feature: dict) -> str:
    raw = str(feature.get("feature_code") or "").strip().upper()
    code = re.sub(r"^ZONE-1F-", "", raw)
    code = re.sub(r"[^A-Z0-9-]+", "-", code).strip("-")[:30]
    if not code:
        raise Floor1CandidatePlanningError("地图区域缺少可生成的稳定区域编号", status_code=409)
    return code


def _point_in_polygon(
    point: tuple[float, float],
    polygon: list[tuple[float, float]],
) -> bool:
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if (
            abs(cross) < 0.0001
            and min(x1, x2) <= x <= max(x1, x2)
            and min(y1, y2) <= y <= max(y1, y2)
        ):
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def _rect_inside_polygon(
    *,
    x: float,
    y: float,
    width: float,
    depth: float,
    polygon: list[tuple[float, float]],
) -> bool:
    return all(
        _point_in_polygon(point, polygon)
        for point in (
            (x, y),
            (x + width, y),
            (x + width, y + depth),
            (x, y + depth),
            (x + width / 2, y + depth / 2),
        )
    )


def _tile_polygon(
    points: list,
    *,
    width_mm: int,
    depth_mm: int,
) -> list[dict]:
    polygon = [(float(point[0]), float(point[1])) for point in points]
    if len(polygon) < 3:
        return []
    min_x = min(point[0] for point in polygon)
    max_x = max(point[0] for point in polygon)
    min_y = min(point[1] for point in polygon)
    max_y = max(point[1] for point in polygon)
    slots: list[dict] = []
    row = 0
    y = min_y
    while y + depth_mm <= max_y + 0.0001:
        column = 0
        x = min_x
        while x + width_mm <= max_x + 0.0001:
            if _rect_inside_polygon(
                x=x,
                y=y,
                width=width_mm,
                depth=depth_mm,
                polygon=polygon,
            ):
                slots.append(
                    {
                        "x_mm": x,
                        "y_mm": y,
                        "width_mm": width_mm,
                        "depth_mm": depth_mm,
                        "row": row + 1,
                        "column": column + 1,
                    }
                )
            x += width_mm
            column += 1
        y += depth_mm
        row += 1
    return slots


def _rectangles_overlap(left: dict, right: dict) -> bool:
    return (
        float(left["x_mm"]) < float(right["max_x"])
        and float(left["x_mm"]) + float(left["width_mm"]) > float(right["min_x"])
        and float(left["y_mm"]) < float(right["max_y"])
        and float(left["y_mm"]) + float(left["depth_mm"]) > float(right["min_y"])
    )


def _segment_obstacle_bounds(points: list, width_mm: float) -> list[dict]:
    half = max(0.0, float(width_mm)) / 2
    bounds: list[dict] = []
    for index in range(len(points) - 1):
        first = points[index]
        second = points[index + 1]
        bounds.append(
            {
                "min_x": min(float(first[0]), float(second[0])) - half,
                "max_x": max(float(first[0]), float(second[0])) + half,
                "min_y": min(float(first[1]), float(second[1])) - half,
                "max_y": max(float(first[1]), float(second[1])) + half,
            }
        )
    return bounds


def _physical_obstacle_bounds(floor_layout: dict) -> list[dict]:
    obstacles: list[dict] = []
    for row in [
        *(floor_layout.get("placements") or []),
        *(floor_layout.get("racks") or []),
    ]:
        if (
            row.get("status") not in {None, "confirmed"}
            or row.get("is_confirmed") is False
        ):
            continue
        width = float(row.get("width_mm") or 0)
        depth = float(row.get("depth_mm") or 0)
        if int(row.get("rotation_deg") or 0) % 180 == 90:
            width, depth = depth, width
        if width <= 0 or depth <= 0:
            continue
        x = float(row.get("x_mm") or 0)
        y = float(row.get("y_mm") or 0)
        obstacles.append(
            {
                "min_x": x - width / 2,
                "max_x": x + width / 2,
                "min_y": y - depth / 2,
                "max_y": y + depth / 2,
            }
        )
    for feature in floor_layout.get("features") or []:
        kind = feature.get("feature_kind")
        subtype = feature.get("subtype")
        points = feature.get("points") or []
        if kind == "aisle" or (kind == "structure" and subtype == "custom_column"):
            obstacles.extend(
                _segment_obstacle_bounds(points, float(feature.get("width_mm") or 0))
            )
        elif kind == "no_go" and len(points) >= 3:
            obstacles.append(
                {
                    "min_x": min(float(point[0]) for point in points),
                    "max_x": max(float(point[0]) for point in points),
                    "min_y": min(float(point[1]) for point in points),
                    "max_y": max(float(point[1]) for point in points),
                }
            )
    return obstacles


def _pallet_slots(points: list, obstacles: list[dict]) -> tuple[list[dict], str]:
    normal = _tile_polygon(
        points,
        width_mm=STANDARD_PALLET_WIDTH_MM,
        depth_mm=STANDARD_PALLET_DEPTH_MM,
    )
    rotated = _tile_polygon(
        points,
        width_mm=STANDARD_PALLET_DEPTH_MM,
        depth_mm=STANDARD_PALLET_WIDTH_MM,
    )
    normal = [
        slot
        for slot in normal
        if not any(_rectangles_overlap(slot, obstacle) for obstacle in obstacles)
    ]
    rotated = [
        slot
        for slot in rotated
        if not any(_rectangles_overlap(slot, obstacle) for obstacle in obstacles)
    ]
    if len(rotated) > len(normal):
        return rotated, "rotated_90"
    return normal, "standard"


def _percent_slot(slot: dict, points: list, floor_bounds: dict) -> dict:
    polygon = [(float(point[0]), float(point[1])) for point in points]
    min_x = min(point[0] for point in polygon)
    max_x = max(point[0] for point in polygon)
    min_y = min(point[1] for point in polygon)
    max_y = max(point[1] for point in polygon)
    width = max_x - min_x
    height = max_y - min_y
    if width <= 0 or height <= 0:
        raise Floor1CandidatePlanningError("一楼区域边界无效，无法生成库位", status_code=409)
    x = float(slot["x_mm"])
    y = float(slot["y_mm"])
    slot_width = float(slot["width_mm"])
    slot_depth = float(slot["depth_mm"])
    if (
        x < float(floor_bounds["min_x"]) - 0.0001
        or y < float(floor_bounds["min_y"]) - 0.0001
        or x + slot_width > float(floor_bounds["max_x"]) + 0.0001
        or y + slot_depth > float(floor_bounds["max_y"]) + 0.0001
    ):
        raise Floor1CandidatePlanningError("区域候选库位超出一楼正式地图边界", status_code=409)
    # Floor3LocationLayout percentages are relative to their bound zone, with
    # top_pct measured downwards from the zone's maximum Y edge.
    left = (x - min_x) / width * 100
    top = (max_y - (y + slot_depth)) / height * 100
    width_pct = slot_width / width * 100
    height_pct = slot_depth / height * 100
    if (
        min(left, top) < -0.0001
        or left + width_pct > 100.0001
        or top + height_pct > 100.0001
    ):
        raise Floor1CandidatePlanningError("候选库位无法映射到所属区域", status_code=409)
    return {
        **slot,
        "left_pct": round(max(0.0, left), 4),
        "top_pct": round(max(0.0, top), 4),
        "width_pct": round(width_pct, 4),
        "height_pct": round(height_pct, 4),
    }


def _zone_inside_floor_bounds(points: list, bounds: dict) -> bool:
    if len(points) < 3:
        return False
    try:
        return all(
            float(bounds["min_x"]) <= float(point[0]) <= float(bounds["max_x"])
            and float(bounds["min_y"]) <= float(point[1]) <= float(bounds["max_y"])
            for point in points
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def build_floor1_formal_candidate_plan(floor_layout: dict) -> dict:
    if str(floor_layout.get("floor_code") or "").upper() != "1F":
        raise Floor1CandidatePlanningError("自动区域候选当前只适用于一楼", status_code=409)
    revision = str(floor_layout.get("revision") or "").strip()
    bounds = floor_layout.get("bounds_mm") or {}
    required_bounds = {"min_x", "min_y", "max_x", "max_y"}
    if not revision or not required_bounds.issubset(bounds):
        raise Floor1CandidatePlanningError("一楼已发布地图缺少版本或毫米边界", status_code=409)

    candidates: list[dict] = []
    excluded_out_of_bounds: list[dict] = list(
        floor_layout.get("excluded_out_of_bounds_zones") or []
    )
    excluded_ids = {
        str(row.get("id") or "") for row in excluded_out_of_bounds if isinstance(row, dict)
    }
    codes: set[str] = set()
    obstacles = _physical_obstacle_bounds(floor_layout)
    for feature in floor_layout.get("features") or []:
        subtype = str(feature.get("subtype") or "")
        if feature.get("feature_kind") != "zone" or feature.get("status") != "confirmed":
            continue
        usage = _USAGE_BY_SUBTYPE.get(subtype)
        if usage is None:
            continue
        area_code = _area_code(feature)
        if area_code in codes:
            raise Floor1CandidatePlanningError(
                f"地图区域自动编号重复：{area_code}", status_code=409
            )
        codes.add(area_code)
        points = feature.get("points") or []
        if not str(feature.get("id") or "").strip() or len(points) < 3:
            raise Floor1CandidatePlanningError(
                f"{area_code} 地图区域缺少稳定标识或有效边界",
                status_code=409,
            )
        if not _zone_inside_floor_bounds(points, bounds):
            feature_id = str(feature.get("id") or "")
            if feature_id not in excluded_ids:
                excluded_out_of_bounds.append(
                    {
                        "id": feature_id,
                        "feature_code": str(feature.get("feature_code") or ""),
                        "name": str(feature.get("name") or area_code),
                        "reason": "outside_measured_bounds",
                    }
                )
                excluded_ids.add(feature_id)
            continue
        slots, orientation = _pallet_slots(points, obstacles)
        is_outdoor = area_code.startswith("OUT-") or str(
            feature.get("id") or ""
        ).startswith("outdoor-")
        is_temporary = usage == "temporary_turnover" or is_outdoor
        is_asset = usage in _ASSET_USAGES
        long_term_eligible = not is_temporary and not is_asset and bool(slots)
        percent_slots = (
            []
            if is_outdoor
            else [_percent_slot(slot, points, bounds) for slot in slots]
        )
        formal_location_slots = (
            percent_slots
            if usage in _INVENTORY_LOCATION_USAGES and long_term_eligible
            else []
        )
        if is_asset:
            capacity_note = "资产区域使用模具/印版台账，区域绑定后不生成纸箱库存库位"
        elif is_temporary:
            capacity_note = "室外或临时周转区域不计入长期安全栈板容量"
        elif not slots:
            capacity_note = "净宽不足以完整放入 1200×1000 mm 标准栈板"
        elif usage == "raw_material":
            capacity_note = "标准栈板容量可确认；原料仍使用原料台账，不生成纸箱库存库位"
        else:
            capacity_note = "确认后生成已放置的成品/半成品正式库位"
        candidates.append(
            {
                "map_feature_id": str(feature.get("id") or ""),
                "feature_code": str(feature.get("feature_code") or ""),
                "area_code": area_code,
                "area_name": str(feature.get("name") or area_code),
                "usage": usage,
                "allowed_inventory_types": [usage],
                "storage_layout": (
                    "rack"
                    if feature.get("storage_mode") == "rack"
                    else "pallet_ground"
                ),
                "pallet_orientation": orientation,
                "measured_pallet_slots": len(slots),
                "planned_pallet_capacity": len(slots) if long_term_eligible else 0,
                "formal_location_count": len(formal_location_slots),
                "long_term_capacity_eligible": long_term_eligible,
                "is_outdoor": is_outdoor,
                "is_temporary": is_temporary,
                "capacity_note": capacity_note,
                "slots": formal_location_slots,
            }
        )

    if not candidates:
        raise Floor1CandidatePlanningError(
            "一楼已发布地图没有可生成的已确认真实区域",
            status_code=409,
        )

    canonical = json.dumps(
        {
            "floor_code": "1F",
            "map_revision": revision,
            "candidates": candidates,
            "excluded_out_of_bounds": excluded_out_of_bounds,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "floor_code": "1F",
        "map_revision": revision,
        "plan_fingerprint": fingerprint,
        "standard_pallet_mm": {
            "width": STANDARD_PALLET_WIDTH_MM,
            "depth": STANDARD_PALLET_DEPTH_MM,
        },
        "candidate_count": len(candidates),
        "excluded_out_of_bounds_count": len(excluded_out_of_bounds),
        "excluded_out_of_bounds": excluded_out_of_bounds,
        "obstacle_count": len(obstacles),
        "formal_location_count": sum(
            row["formal_location_count"] for row in candidates
        ),
        "long_term_pallet_capacity": sum(
            row["planned_pallet_capacity"] for row in candidates
        ),
        "candidates": candidates,
        "inventory_changed": False,
        "confirmation_required": True,
    }


def _candidate_state_exists(
    db: Session,
    *,
    floor: WarehouseFloor,
    plan: dict,
) -> bool:
    for candidate in plan["candidates"]:
        area = db.scalar(
            select(WarehouseArea)
            .options(selectinload(WarehouseArea.storage_policy))
            .where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == candidate["area_code"],
            )
        )
        if area is None or area.storage_policy is None:
            return False
        policy = area.storage_policy
        if (
            policy.map_feature_id != candidate["map_feature_id"]
            or policy.allowed_inventory_types_json
            != json.dumps(
                candidate["allowed_inventory_types"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            or policy.storage_layout != candidate["storage_layout"]
            or policy.status != "published"
            or policy.published_map_revision != plan["map_revision"]
            or area.construction_status != "enabled"
            or area.planned_location_count != candidate["formal_location_count"]
            or area.planned_pallet_capacity != candidate["planned_pallet_capacity"]
            or area.capacity_review_status
            != ("confirmed" if candidate["long_term_capacity_eligible"] else "excluded")
            or area.capacity_eligible
            != bool(candidate["long_term_capacity_eligible"])
            or area.confirmed_pallet_capacity
            != (
                candidate["planned_pallet_capacity"]
                if candidate["long_term_capacity_eligible"]
                else None
            )
        ):
            return False
        rows = list(
            db.scalars(
                select(WarehouseLocation)
                .options(selectinload(WarehouseLocation.floor3_layout))
                .where(
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    WarehouseLocation.area_code == area.area_code,
                    WarehouseLocation.source_version == AREA_LOCATION_SOURCE_VERSION,
                    WarehouseLocation.is_active.is_(True),
                )
                .order_by(WarehouseLocation.location_code)
            ).all()
        )
        if len(rows) != candidate["formal_location_count"] or any(
            row.placement_status != "placed" for row in rows
        ):
            return False
        for serial, (row, slot) in enumerate(zip(rows, candidate["slots"]), start=1):
            expected_code = (
                f"{floor.floor_code.upper()}-{area.area_code}-L{serial:03d}"
            )
            layout = row.floor3_layout
            if (
                row.location_code != expected_code
                or row.warehouse_type != candidate["usage"]
                or row.storage_type
                != ("rack" if candidate["storage_layout"] == "rack" else "ground")
                or layout is None
                or abs(float(layout.left_pct) - slot["left_pct"]) > 0.0001
                or abs(float(layout.top_pct) - slot["top_pct"]) > 0.0001
                or abs(float(layout.width_pct) - slot["width_pct"]) > 0.0001
                or abs(float(layout.height_pct) - slot["height_pct"]) > 0.0001
            ):
                return False
    return True


def inspect_floor1_formal_candidate_state(
    db: Session,
    *,
    plan: dict,
) -> dict:
    floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_number == 1))
    if floor is None:
        raise Floor1CandidatePlanningError("一楼正式楼层台账不存在", status_code=409)
    candidate_codes = {row["area_code"] for row in plan["candidates"]}
    all_areas = list(
        db.scalars(
            select(WarehouseArea)
            .options(selectinload(WarehouseArea.storage_policy))
            .where(WarehouseArea.floor_id == floor.id)
            .order_by(WarehouseArea.area_code, WarehouseArea.id)
        ).all()
    )
    candidate_areas = [area for area in all_areas if area.area_code in candidate_codes]
    already_applied = _candidate_state_exists(db, floor=floor, plan=plan)
    blockers: list[str] = []
    if candidate_areas and not already_applied:
        blockers.append("自动候选区域已存在部分正式台账或位置，不能静默覆盖")

    legacy_areas: list[dict] = []
    for area in all_areas:
        if area.area_code in candidate_codes:
            continue
        locations = list(
            db.scalars(
                select(WarehouseLocation)
                .where(
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
                )
                .order_by(WarehouseLocation.id)
            ).all()
        )
        location_ids = [row.id for row in locations]
        live_lot_count = 0
        current_pallet_count = 0
        if location_ids:
            live_lot_count = int(
                db.scalar(
                    select(func.count(InventoryLot.id)).where(
                        InventoryLot.warehouse_location_id.in_(location_ids),
                        InventoryLot.status.in_(("active", "frozen")),
                        (
                            InventoryLot.quantity_available
                            + InventoryLot.quantity_reserved
                            + InventoryLot.quantity_damaged
                        )
                        > 0,
                    )
                )
                or 0
            )
            current_pallet_count = int(
                db.scalar(
                    select(func.count(InventoryPallet.id)).where(
                        InventoryPallet.location_id.in_(location_ids),
                        InventoryPallet.is_current.is_(True),
                    )
                )
                or 0
            )
        active_location_count = sum(1 for row in locations if row.is_active)
        has_blocker = bool(
            area.storage_policy is not None or live_lot_count or current_pallet_count
        )
        if area.storage_policy is not None:
            blockers.append(f"{area.area_code} 历史区域仍绑定正式地图策略")
        if live_lot_count or current_pallet_count:
            blockers.append(
                f"{area.area_code} 历史区域仍有库存或实体栈板，不能自动归档"
            )
        archive_required = not has_blocker and bool(
            area.construction_status == "enabled"
            or active_location_count
            or area.planned_location_count
            or area.planned_pallet_capacity
            or area.capacity_review_status != "excluded"
        )
        legacy_areas.append(
            {
                "area_id": area.id,
                "area_code": area.area_code,
                "area_name": area.area_name,
                "construction_status": area.construction_status,
                "location_ids": location_ids,
                "active_location_count": active_location_count,
                "live_lot_count": live_lot_count,
                "current_pallet_count": current_pallet_count,
                "has_formal_policy": area.storage_policy is not None,
                "archive_required": archive_required,
                "action": (
                    "block"
                    if has_blocker
                    else "archive_empty_legacy"
                    if archive_required
                    else "keep_archived_history"
                ),
            }
        )

    canonical = json.dumps(
        {
            "map_revision": plan["map_revision"],
            "already_applied": already_applied,
            "candidate_area_ids": [area.id for area in candidate_areas],
            "legacy_areas": legacy_areas,
            "blocking_conflicts": blockers,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "already_applied": already_applied,
        "archivable_legacy_area_count": sum(
            1 for row in legacy_areas if row["archive_required"]
        ),
        "legacy_areas": legacy_areas,
        "blocking_conflicts": blockers,
    }


def _ensure_no_candidate_conflicts(
    db: Session,
    *,
    floor: WarehouseFloor,
    plan: dict,
) -> None:
    feature_ids = [row["map_feature_id"] for row in plan["candidates"]]
    area_codes = [row["area_code"] for row in plan["candidates"]]
    existing_areas = list(
        db.scalars(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code.in_(area_codes),
            )
        ).all()
    )
    existing_policies = list(
        db.scalars(
            select(WarehouseAreaStoragePolicy).where(
                WarehouseAreaStoragePolicy.map_feature_id.in_(feature_ids)
            )
        ).all()
    )
    if existing_areas or existing_policies:
        raise Floor1CandidatePlanningError(
            "候选区域已存在部分正式绑定，系统不会静默覆盖；请先核对冲突后再确认",
            status_code=409,
        )


def confirm_floor1_formal_candidate_plan(
    db: Session,
    *,
    floor_layout: dict,
    expected_revision: str,
    expected_fingerprint: str,
    expected_formal_state_fingerprint: str,
    operator_id: int,
    reviewer_name: str,
) -> Floor1CandidateApplyResult:
    plan = build_floor1_formal_candidate_plan(floor_layout)
    if plan["map_revision"] != expected_revision:
        raise Floor1CandidatePlanningError("一楼地图版本已变化，请刷新候选后重新确认", status_code=409)
    if plan["plan_fingerprint"] != expected_fingerprint:
        raise Floor1CandidatePlanningError("一楼候选测算结果已变化，请刷新后重新确认", status_code=409)
    floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_number == 1))
    if floor is None:
        raise Floor1CandidatePlanningError("一楼正式楼层台账不存在", status_code=409)
    if floor.construction_status != "enabled":
        raise Floor1CandidatePlanningError(
            "一楼正式楼层尚未启用，请先核对楼层台账",
            status_code=409,
        )
    formal_state = inspect_floor1_formal_candidate_state(db, plan=plan)
    if (
        formal_state["already_applied"]
        and not formal_state["blocking_conflicts"]
        and not formal_state["archivable_legacy_area_count"]
    ):
        areas = tuple(
            db.scalars(
                select(WarehouseArea).where(
                    WarehouseArea.floor_id == floor.id,
                    WarehouseArea.area_code.in_(
                        [row["area_code"] for row in plan["candidates"]]
                    ),
                )
            ).all()
        )
        return Floor1CandidateApplyResult(
            plan=plan,
            applied=False,
            areas=areas,
            locations=(),
            archived_legacy_areas=(),
        )
    if formal_state["fingerprint"] != expected_formal_state_fingerprint:
        raise Floor1CandidatePlanningError(
            "一楼正式区域台账已变化，请刷新候选后重新确认",
            status_code=409,
        )
    if formal_state["blocking_conflicts"]:
        raise Floor1CandidatePlanningError(
            "；".join(formal_state["blocking_conflicts"][:5]),
            status_code=409,
        )

    now = beijing_now_naive()
    archived_legacy_areas: list[WarehouseArea] = []
    for legacy in formal_state["legacy_areas"]:
        if not legacy["archive_required"]:
            continue
        area = db.get(WarehouseArea, legacy["area_id"])
        if area is None:
            raise Floor1CandidatePlanningError(
                "历史区域台账已变化，请刷新后重试",
                status_code=409,
            )
        rows = list(
            db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.id.in_(legacy["location_ids"])
                )
            ).all()
        )
        for row in rows:
            row.is_active = False
        area.planned_location_count = 0
        area.planned_pallet_capacity = 0
        area.construction_status = "ledger_building"
        area.capacity_review_status = "excluded"
        area.capacity_eligible = False
        area.confirmed_pallet_capacity = None
        area.capacity_reviewed_by = reviewer_name
        area.capacity_reviewed_at = now
        archive_note = "P1-42 一楼实测区域确认时归档为空的未映射历史区域"
        area.remarks = (
            f"{area.remarks}\n{archive_note}" if area.remarks else archive_note
        )
        archived_legacy_areas.append(area)

    if _candidate_state_exists(db, floor=floor, plan=plan):
        areas = tuple(
            db.scalars(
                select(WarehouseArea).where(
                    WarehouseArea.floor_id == floor.id,
                    WarehouseArea.area_code.in_(
                        [row["area_code"] for row in plan["candidates"]]
                    ),
                )
            ).all()
        )
        return Floor1CandidateApplyResult(
            plan=plan,
            applied=bool(archived_legacy_areas),
            areas=areas,
            locations=(),
            archived_legacy_areas=tuple(archived_legacy_areas),
        )
    _ensure_no_candidate_conflicts(db, floor=floor, plan=plan)

    next_sort = int(db.scalar(select(func.max(WarehouseLocation.sort_order))) or 0) + 1
    areas: list[WarehouseArea] = []
    locations: list[WarehouseLocation] = []
    for candidate in plan["candidates"]:
        eligible = bool(candidate["long_term_capacity_eligible"])
        area = WarehouseArea(
            floor_id=floor.id,
            area_code=candidate["area_code"],
            area_name=candidate["area_name"],
            planned_location_count=candidate["formal_location_count"],
            planned_pallet_capacity=candidate["planned_pallet_capacity"],
            construction_status="enabled",
            capacity_review_status="confirmed" if eligible else "excluded",
            capacity_eligible=eligible,
            confirmed_pallet_capacity=(
                candidate["planned_pallet_capacity"] if eligible else None
            ),
            capacity_reviewed_by=reviewer_name,
            capacity_reviewed_at=now,
            remarks=candidate["capacity_note"],
        )
        db.add(area)
        db.flush()
        area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id=candidate["map_feature_id"],
            allowed_inventory_types_json=json.dumps(
                candidate["allowed_inventory_types"],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            storage_layout=candidate["storage_layout"],
            status="published",
            published_map_revision=plan["map_revision"],
            version=1,
            updated_by=operator_id,
        )
        db.flush()
        for serial, slot in enumerate(candidate["slots"], start=1):
            row = WarehouseLocation(
                location_code=f"{floor.floor_code.upper()}-{area.area_code}-L{serial:03d}",
                location_name=f"{area.area_name} {serial:03d} 号位",
                warehouse_type=candidate["usage"],
                warehouse_floor=floor.floor_number,
                area_code=area.area_code,
                storage_type=(
                    "rack" if candidate["storage_layout"] == "rack" else "ground"
                ),
                sort_order=next_sort,
                is_temporary=False,
                source_version=AREA_LOCATION_SOURCE_VERSION,
                placement_status="placed",
            )
            row.floor3_layout = Floor3LocationLayout(
                left_pct=Decimal(str(slot["left_pct"])),
                top_pct=Decimal(str(slot["top_pct"])),
                width_pct=Decimal(str(slot["width_pct"])),
                height_pct=Decimal(str(slot["height_pct"])),
                z_index=0,
                version=1,
                source_type="manual",
                created_by=operator_id,
                updated_by=operator_id,
            )
            db.add(row)
            locations.append(row)
            next_sort += 1
        areas.append(area)
    db.flush()
    return Floor1CandidateApplyResult(
        plan=plan,
        applied=True,
        areas=tuple(areas),
        locations=tuple(locations),
        archived_legacy_areas=tuple(archived_legacy_areas),
    )


def overlay_formal_area_bindings(
    db: Session,
    *,
    floor_code: str,
    floor_layout: dict,
    include_draft: bool = False,
) -> dict:
    floor = db.scalar(
        select(WarehouseFloor).where(
            func.upper(WarehouseFloor.floor_code) == floor_code.strip().upper()
        )
    )
    if floor is None:
        floor = db.scalar(
            select(WarehouseFloor).where(
                WarehouseFloor.floor_number == int(re.sub(r"\D", "", floor_code) or 0)
            )
        )
    if floor is None:
        return floor_layout
    policy_query = (
        select(WarehouseAreaStoragePolicy)
        .join(WarehouseArea)
        .options(selectinload(WarehouseAreaStoragePolicy.area))
        .where(WarehouseArea.floor_id == floor.id)
    )
    if not include_draft:
        policy_query = policy_query.where(
            WarehouseAreaStoragePolicy.status == "published"
        )
    policies = list(
        db.scalars(
            policy_query
        ).all()
    )
    areas_by_code = {
        area.area_code.upper(): area
        for area in db.scalars(
            select(WarehouseArea).where(WarehouseArea.floor_id == floor.id)
        ).all()
    }
    areas_by_id = {area.id: area for area in areas_by_code.values()}
    has_draft = bool((floor_layout.get("draft_control") or {}).get("has_draft"))
    by_feature = {policy.map_feature_id: policy for policy in policies}
    features: list[dict] = []
    for raw in floor_layout.get("features") or []:
        feature = dict(raw)
        policy = by_feature.get(str(feature.get("id") or ""))
        if policy is not None:
            policy_types = json.loads(policy.allowed_inventory_types_json)
            feature["formal_area_id"] = policy.area.id
            feature["formal_floor_id"] = policy.area.floor_id
            feature["formal_policy_status"] = policy.status
            if include_draft:
                feature.setdefault("erp_area_code", policy.area.area_code)
                feature.setdefault("allowed_inventory_types", policy_types)
                feature.setdefault("storage_layout", policy.storage_layout)
                feature.setdefault("formal_area_name", policy.area.area_name)
                feature["formal_binding_status"] = policy.status
            else:
                feature["erp_area_code"] = policy.area.area_code
                feature["allowed_inventory_types"] = policy_types
                feature["storage_layout"] = policy.storage_layout
                feature["formal_binding_status"] = policy.status
                feature['formal_area_name'] = policy.area.area_name
            feature['formal_construction_status'] = policy.area.construction_status
            feature['planned_location_count'] = policy.area.planned_location_count
            feature['planned_pallet_capacity'] = policy.area.planned_pallet_capacity
            feature['capacity_review_status'] = policy.area.capacity_review_status
            feature['capacity_eligible'] = policy.area.capacity_eligible
            feature['confirmed_pallet_capacity'] = policy.area.confirmed_pallet_capacity
            feature['capacity_reviewed_by'] = policy.area.capacity_reviewed_by
            feature['capacity_reviewed_at'] = (
                policy.area.capacity_reviewed_at.isoformat()
                if policy.area.capacity_reviewed_at else None
            )
        elif (
            include_draft
            and has_draft
            and str(feature.get("erp_area_code") or "").strip()
        ):
            feature["formal_binding_status"] = "draft"
            draft_area_id = feature.get("formal_area_id")
            draft_area = (
                areas_by_id.get(int(draft_area_id))
                if isinstance(draft_area_id, int) or str(draft_area_id or "").isdigit()
                else None
            )
            if draft_area is not None:
                draft_code = str(feature.get("erp_area_code") or "").strip().upper()
                if draft_area.floor_id != floor.id or draft_area.area_code.upper() != draft_code:
                    feature["formal_identity_status"] = "drifted"
                    features.append(feature)
                    continue
                feature["formal_construction_status"] = draft_area.construction_status
                feature["planned_pallet_capacity"] = draft_area.planned_pallet_capacity
                feature["capacity_review_status"] = draft_area.capacity_review_status
                feature["capacity_eligible"] = draft_area.capacity_eligible
                feature["confirmed_pallet_capacity"] = draft_area.confirmed_pallet_capacity
        features.append(feature)
    return {**floor_layout, "features": features}

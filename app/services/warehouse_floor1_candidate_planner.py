from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
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
from app.services.warehouse_area_activation import (
    AREA_LOCATION_SOURCE_VERSION,
    floor3_v11_map_binding_is_proven,
    legacy_v11_area_policy_projection,
)
from app.services.warehouse_pallet_standard import (
    STANDARD_PALLET_DEPTH_MM,
    STANDARD_PALLET_WIDTH_MM,
)
from app.services.warehouse_location_address import employee_area_name
from app.services.warehouse_area_tombstones import (
    archived_area_tombstones_for_floor,
    filter_archived_area_layout,
)


LOGICAL_ANCHOR_FOOTPRINT_MM = 400
# Percent coordinates are persisted with four decimal places.  A millimetre
# round-trip error grows with the measured zone span.  Keep a small base for
# floating-point ulps, then scale to more than the worst two-edge quantisation
# error (0.0001 percent per persisted value).
_GEOMETRY_EPSILON_MM = 0.01
_PERCENT_ROUND_TRIP_EPSILON_FACTOR = 0.0000025
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
_PRESERVED_BUSINESS_ANCHOR_AREA_CODES = frozenset(
    {"DISPATCH", "FIN-001", "FIN-002", "FIN-003"}
)


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
    *,
    epsilon_mm: float = _GEOMETRY_EPSILON_MM,
) -> bool:
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        segment_length = math.hypot(x2 - x1, y2 - y1)
        if (
            abs(cross) <= epsilon_mm * max(1.0, segment_length)
            and min(x1, x2) - epsilon_mm
            <= x
            <= max(x1, x2) + epsilon_mm
            and min(y1, y2) - epsilon_mm
            <= y
            <= max(y1, y2) + epsilon_mm
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
    epsilon_mm: float = _GEOMETRY_EPSILON_MM,
) -> bool:
    rectangle = (
        (x, y),
        (x + width, y),
        (x + width, y + depth),
        (x, y + depth),
    )
    if not all(
        _point_in_polygon(point, polygon, epsilon_mm=epsilon_mm)
        for point in (*rectangle, (x + width / 2, y + depth / 2))
    ):
        return False

    def cross(
        first: tuple[float, float],
        second: tuple[float, float],
        third: tuple[float, float],
    ) -> float:
        return (
            (second[0] - first[0]) * (third[1] - first[1])
            - (second[1] - first[1]) * (third[0] - first[0])
        )

    rectangle_edges = [
        (rectangle[index], rectangle[(index + 1) % len(rectangle)])
        for index in range(len(rectangle))
    ]
    polygon_edges = [
        (polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    ]
    for rect_start, rect_end in rectangle_edges:
        for poly_start, poly_end in polygon_edges:
            first = cross(rect_start, rect_end, poly_start)
            second = cross(rect_start, rect_end, poly_end)
            third = cross(poly_start, poly_end, rect_start)
            fourth = cross(poly_start, poly_end, rect_end)
            if first * second < -0.0001 and third * fourth < -0.0001:
                return False
    # A concave notch can enter and leave through points on the rectangle edge
    # without producing a strict segment crossing.  A polygon vertex inside
    # the rectangle still proves that part of its footprint lies outside.
    if any(
        x + 0.0001 < point_x < x + width - 0.0001
        and y + 0.0001 < point_y < y + depth - 0.0001
        for point_x, point_y in polygon
    ):
        return False
    return True


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


def _rectangles_overlap(
    left: dict,
    right: dict,
    *,
    epsilon_mm: float = _GEOMETRY_EPSILON_MM,
) -> bool:
    left_min_x = float(left.get("min_x", left.get("x_mm", 0)))
    left_min_y = float(left.get("min_y", left.get("y_mm", 0)))
    left_max_x = float(
        left["max_x"]
        if "max_x" in left
        else left_min_x + float(left.get("width_mm", 0))
    )
    left_max_y = float(
        left["max_y"]
        if "max_y" in left
        else left_min_y + float(left.get("depth_mm", 0))
    )
    right_min_x = float(right.get("min_x", right.get("x_mm", 0)))
    right_min_y = float(right.get("min_y", right.get("y_mm", 0)))
    right_max_x = float(
        right["max_x"]
        if "max_x" in right
        else right_min_x + float(right.get("width_mm", 0))
    )
    right_max_y = float(
        right["max_y"]
        if "max_y" in right
        else right_min_y + float(right.get("depth_mm", 0))
    )
    return (
        left_min_x < right_max_x - epsilon_mm
        and left_max_x > right_min_x + epsilon_mm
        and left_min_y < right_max_y - epsilon_mm
        and left_max_y > right_min_y + epsilon_mm
    )


def _percent_round_trip_epsilon(points: list) -> float:
    polygon = [(float(point[0]), float(point[1])) for point in points]
    if not polygon:
        return _GEOMETRY_EPSILON_MM
    span = max(
        max(point[0] for point in polygon) - min(point[0] for point in polygon),
        max(point[1] for point in polygon) - min(point[1] for point in polygon),
    )
    return max(
        _GEOMETRY_EPSILON_MM,
        span * _PERCENT_ROUND_TRIP_EPSILON_FACTOR,
    )


def _segment_obstacle_bounds(points: list, width_mm: float) -> list[dict]:
    half = max(0.0, float(width_mm)) / 2
    bounds: list[dict] = []
    for index in range(len(points) - 1):
        first = points[index]
        second = points[index + 1]
        dx = float(second[0]) - float(first[0])
        dy = float(second[1]) - float(first[1])
        length = math.hypot(dx, dy)
        if length < 1:
            continue
        # Match the rendered segment: width extends along its normal only,
        # never beyond its endpoints along the direction of travel.
        pad_x = abs(dy / length * half)
        pad_y = abs(dx / length * half)
        bounds.append(
            {
                "min_x": min(float(first[0]), float(second[0])) - pad_x,
                "max_x": max(float(first[0]), float(second[0])) + pad_x,
                "min_y": min(float(first[1]), float(second[1])) - pad_y,
                "max_y": max(float(first[1]), float(second[1])) + pad_y,
            }
        )
    return bounds


def _physical_obstacle_bounds(floor_layout: dict) -> list[dict]:
    obstacles: list[dict] = []
    for structure in floor_layout.get("structures") or []:
        if structure.get("kind") != "column":
            continue
        geometry = structure.get("geometry") or {}
        if geometry.get("type") == "circle":
            radius = float(geometry.get("radius_mm") or 0)
            if radius <= 0:
                continue
            x = float(geometry.get("x_mm") or 0)
            y = float(geometry.get("y_mm") or 0)
            obstacles.append(
                {
                    "min_x": x - radius,
                    "max_x": x + radius,
                    "min_y": y - radius,
                    "max_y": y + radius,
                }
            )
        elif geometry.get("type") == "polyline":
            points = geometry.get("points") or []
            if len(points) >= 3:
                obstacles.append(
                    {
                        "min_x": min(float(point[0]) for point in points),
                        "max_x": max(float(point[0]) for point in points),
                        "min_y": min(float(point[1]) for point in points),
                        "max_y": max(float(point[1]) for point in points),
                    }
                )
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
        # The measured floor envelope is the passage surface. Zones carve out
        # storage space, so legacy drawn aisle lines no longer block locations
        # that are validly contained by their own zone. Keep those source
        # features in the layout for history and rollback.
        if kind == "structure" and subtype == "custom_column":
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


def _slot_center(slot: dict) -> tuple[float, float]:
    return (
        float(slot["x_mm"]) + float(slot["width_mm"]) / 2,
        float(slot["y_mm"]) + float(slot["depth_mm"]) / 2,
    )


def _distributed_slots(
    slots: list[dict],
    *,
    target_count: int,
    anchor_slots: list[dict] | None = None,
) -> list[dict]:
    """Deterministically spread a subset across all usable space.

    The old implementation returned the row-major prefix.  Max-min sampling
    keeps the result stable while making every next location as far as
    possible from already selected/fixed locations.
    """

    if target_count <= 0:
        return []
    if len(slots) < target_count:
        return []
    remaining = sorted(
        slots,
        key=lambda slot: (
            round(_slot_center(slot)[1], 6),
            round(_slot_center(slot)[0], 6),
            round(float(slot["width_mm"]), 6),
            round(float(slot["depth_mm"]), 6),
        ),
    )
    anchor_centers = [_slot_center(slot) for slot in (anchor_slots or [])]
    selected: list[dict] = []
    selected_centers: list[tuple[float, float]] = []

    if not anchor_centers:
        centroid_x = sum(_slot_center(slot)[0] for slot in remaining) / len(remaining)
        centroid_y = sum(_slot_center(slot)[1] for slot in remaining) / len(remaining)
        first = min(
            remaining,
            key=lambda slot: (
                (_slot_center(slot)[0] - centroid_x) ** 2
                + (_slot_center(slot)[1] - centroid_y) ** 2,
                round(_slot_center(slot)[1], 6),
                round(_slot_center(slot)[0], 6),
            ),
        )
        remaining.remove(first)
        selected.append(first)
        selected_centers.append(_slot_center(first))

    reference_centers = [*anchor_centers, *selected_centers]
    nearest_distances = [
        min(
            (center_x - anchor_x) ** 2 + (center_y - anchor_y) ** 2
            for anchor_x, anchor_y in reference_centers
        )
        for center_x, center_y in map(_slot_center, remaining)
    ]
    while remaining and len(selected) < target_count:
        candidate_index = max(
            range(len(remaining)),
            key=lambda index: (
                nearest_distances[index],
                -round(_slot_center(remaining[index])[1], 6),
                -round(_slot_center(remaining[index])[0], 6),
            ),
        )
        candidate = remaining.pop(candidate_index)
        nearest_distances.pop(candidate_index)
        selected.append(candidate)
        candidate_center = _slot_center(candidate)
        selected_centers.append(candidate_center)
        for index, slot in enumerate(remaining):
            center = _slot_center(slot)
            distance = (
                (center[0] - candidate_center[0]) ** 2
                + (center[1] - candidate_center[1]) ** 2
            )
            nearest_distances[index] = min(nearest_distances[index], distance)
    return selected


def _percent_geometry_to_slot(slot: dict, points: list) -> dict:
    polygon = [(float(point[0]), float(point[1])) for point in points]
    min_x = min(point[0] for point in polygon)
    max_x = max(point[0] for point in polygon)
    min_y = min(point[1] for point in polygon)
    max_y = max(point[1] for point in polygon)
    width = max_x - min_x
    height = max_y - min_y
    epsilon_mm = _percent_round_trip_epsilon(points)
    left_pct = float(slot["left_pct"])
    top_pct = float(slot["top_pct"])
    width_pct = float(slot["width_pct"])
    height_pct = float(slot["height_pct"])
    slot_width = width * width_pct / 100
    slot_depth = height * height_pct / 100
    slot_x = min_x + width * left_pct / 100
    slot_y = max_y - height * top_pct / 100 - slot_depth
    if abs(slot_x - min_x) <= epsilon_mm:
        slot_x = min_x
    if abs(slot_y - min_y) <= epsilon_mm:
        slot_y = min_y
    if abs(slot_x + slot_width - max_x) <= epsilon_mm:
        slot_width = max_x - slot_x
    if abs(slot_y + slot_depth - max_y) <= epsilon_mm:
        slot_depth = max_y - slot_y
    layout_kind = str(slot.get("layout_kind") or "unknown")
    represents_physical_pallet = (
        1080 <= slot_width <= 1320
        and 900 <= slot_depth <= 1100
    ) or (
        900 <= slot_width <= 1100
        and 1080 <= slot_depth <= 1320
    )
    is_logical_anchor = layout_kind == "logical_anchor" or (
        layout_kind != "physical_pallet"
        and slot_width > 0
        and slot_depth > 0
        and not represents_physical_pallet
    )
    if is_logical_anchor:
        effective_width = min(slot_width, LOGICAL_ANCHOR_FOOTPRINT_MM)
        effective_depth = min(slot_depth, LOGICAL_ANCHOR_FOOTPRINT_MM)
        slot_x += (slot_width - effective_width) / 2
        slot_y += (slot_depth - effective_depth) / 2
        slot_width = effective_width
        slot_depth = effective_depth
    return {
        **slot,
        "x_mm": slot_x,
        "y_mm": slot_y,
        "width_mm": slot_width,
        "depth_mm": slot_depth,
    }


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


def measured_pallet_slots_for_zone(
    floor_layout: dict,
    *,
    feature_id: str,
) -> list[dict]:
    """Return real 1200x1000 pallet slots inside one measured map zone."""

    bounds = floor_layout.get("bounds_mm") or {}
    required_bounds = {"min_x", "min_y", "max_x", "max_y"}
    if not required_bounds.issubset(bounds):
        raise Floor1CandidatePlanningError("实测地图缺少毫米边界，无法生成空货位", status_code=409)
    feature = next(
        (
            row
            for row in floor_layout.get("features") or []
            if row.get("feature_kind") == "zone"
            and str(row.get("id") or "") == str(feature_id or "")
        ),
        None,
    )
    if feature is None:
        raise Floor1CandidatePlanningError("实测地图区域不存在", status_code=404)
    points = feature.get("points") or []
    if len(points) < 3 or not _zone_inside_floor_bounds(points, bounds):
        raise Floor1CandidatePlanningError("实测区域边界无效，无法生成空货位", status_code=409)
    slots, _orientation = _pallet_slots(points, _physical_obstacle_bounds(floor_layout))
    return [_percent_slot(slot, points, bounds) for slot in slots]


def confirmed_capacity_slots_for_zone(
    floor_layout: dict,
    *,
    feature_id: str,
    target_count: int,
    prefer_standard_pallet_slots: bool = True,
    reserved_slots: list[dict] | None = None,
) -> list[dict]:
    """Create placed logical positions for an administrator-confirmed capacity.

    The 1200x1000 fit calculation remains the default suggestion.  It is not a
    veto over a manager's measured-site confirmation: narrow, long, irregular
    and rack zones still need stable selectable positions for inbound, moves
    and outbound work.  When the confirmed count exceeds the standard fit, a
    deterministic compact grid is generated inside the measured polygon.
    These rectangles are UI anchors, not a claim that every icon is a full
    pallet footprint.
    """

    if target_count < 0 or target_count > 500:
        raise Floor1CandidatePlanningError("确认容量必须在 0 到 500 之间", status_code=409)
    if target_count == 0:
        return []
    bounds = floor_layout.get("bounds_mm") or {}
    required_bounds = {"min_x", "min_y", "max_x", "max_y"}
    if not required_bounds.issubset(bounds):
        raise Floor1CandidatePlanningError(
            "实测地图缺少毫米边界，无法生成确认容量位置", status_code=409
        )
    feature = next(
        (
            row
            for row in floor_layout.get("features") or []
            if row.get("feature_kind") == "zone"
            and str(row.get("id") or "") == str(feature_id or "")
        ),
        None,
    )
    if feature is None:
        raise Floor1CandidatePlanningError("实测地图区域不存在", status_code=404)
    points = feature.get("points") or []
    if len(points) < 3 or not _zone_inside_floor_bounds(points, bounds):
        raise Floor1CandidatePlanningError(
            "实测区域边界无效，无法生成确认容量位置", status_code=409
        )
    polygon = [(float(point[0]), float(point[1])) for point in points]
    min_x = min(point[0] for point in polygon)
    max_x = max(point[0] for point in polygon)
    min_y = min(point[1] for point in polygon)
    max_y = max(point[1] for point in polygon)
    width = max_x - min_x
    height = max_y - min_y
    geometry_epsilon = _percent_round_trip_epsilon(points)
    if width <= 0 or height <= 0:
        raise Floor1CandidatePlanningError(
            "实测区域边界无效，无法生成确认容量位置", status_code=409
        )

    reserved_physical = [
        _percent_geometry_to_slot(slot, points) for slot in (reserved_slots or [])
    ]
    if prefer_standard_pallet_slots:
        measured = measured_pallet_slots_for_zone(
            floor_layout, feature_id=feature_id
        )
        measured = [
            slot
            for slot in measured
            if not any(
                _rectangles_overlap(
                    slot,
                    reserved,
                    epsilon_mm=geometry_epsilon,
                )
                for reserved in reserved_physical
            )
        ]
        if len(measured) >= target_count:
            selected = _distributed_slots(
                measured,
                target_count=target_count,
                anchor_slots=reserved_physical,
            )
            return selected

    aspect = max(0.05, min(20.0, width / height))
    obstacles = _physical_obstacle_bounds(floor_layout)
    for density in range(3, 25):
        cell_count = max(
            (target_count + len(reserved_physical)) * density,
            16,
        )
        columns = max(1, int(math.ceil(math.sqrt(cell_count * aspect))))
        rows = max(1, int(math.ceil(cell_count / columns)))
        cell_width = width / columns
        cell_height = height / rows
        # Logical positions are operational anchors, not full pallet claims.
        # Generate and validate the same <=400 mm footprint rendered by the UI.
        slot_width = min(cell_width * 0.68, LOGICAL_ANCHOR_FOOTPRINT_MM)
        slot_depth = min(cell_height * 0.68, LOGICAL_ANCHOR_FOOTPRINT_MM)
        slots: list[dict] = []
        for row_index in range(rows):
            center_y = min_y + (row_index + 0.5) * cell_height
            for column_index in range(columns):
                center_x = min_x + (column_index + 0.5) * cell_width
                if not _point_in_polygon(
                    (center_x, center_y),
                    polygon,
                    epsilon_mm=geometry_epsilon,
                ):
                    continue
                current_width = slot_width
                current_depth = slot_depth
                candidate = None
                for _attempt in range(8):
                    left = center_x - current_width / 2
                    bottom = center_y - current_depth / 2
                    if _rect_inside_polygon(
                        x=left,
                        y=bottom,
                        width=current_width,
                        depth=current_depth,
                        polygon=polygon,
                        epsilon_mm=geometry_epsilon,
                    ):
                        possible = {
                            "x_mm": left,
                            "y_mm": bottom,
                            "width_mm": current_width,
                            "depth_mm": current_depth,
                            "row": row_index + 1,
                            "column": column_index + 1,
                            "capacity_confirmed": True,
                        }
                        if not any(
                            _rectangles_overlap(
                                possible,
                                obstacle,
                                epsilon_mm=geometry_epsilon,
                            )
                            for obstacle in [*obstacles, *reserved_physical]
                        ):
                            candidate = possible
                            break
                    current_width *= 0.72
                    current_depth *= 0.72
                if candidate is not None:
                    slots.append(_percent_slot(candidate, points, bounds))
        if len(slots) >= target_count:
            return _distributed_slots(
                slots,
                target_count=target_count,
                anchor_slots=reserved_physical,
            )
    raise Floor1CandidatePlanningError(
        "实测区域边界过窄或无效，无法生成已确认容量位置", status_code=409
    )


def validate_capacity_layout_slots_for_zone(
    floor_layout: dict,
    *,
    feature_id: str,
    slots: list[dict],
    allow_spatial_conflicts: bool = False,
) -> list[dict]:
    """Validate one complete active ground-location layout against the map.

    Percentage bounds alone are insufficient for irregular zones.  This
    converts every location back to measured millimetres and rejects a whole
    batch when a rectangle crosses the zone, hits a physical/no-go obstacle,
    or overlaps another location.
    """

    feature = next(
        (
            row
            for row in floor_layout.get("features") or []
            if row.get("feature_kind") == "zone"
            and str(row.get("id") or "") == str(feature_id or "")
        ),
        None,
    )
    if feature is None:
        raise Floor1CandidatePlanningError("实测地图区域不存在", status_code=404)
    points = feature.get("points") or []
    bounds = floor_layout.get("bounds_mm") or {}
    if len(points) < 3 or not _zone_inside_floor_bounds(points, bounds):
        raise Floor1CandidatePlanningError(
            "实测区域边界无效，无法保存货位位置", status_code=409
        )
    polygon = [(float(point[0]), float(point[1])) for point in points]
    geometry_epsilon = _percent_round_trip_epsilon(points)
    obstacles = _physical_obstacle_bounds(floor_layout)
    physical: list[dict] = []
    for slot in slots:
        try:
            current = _percent_geometry_to_slot(slot, points)
        except (KeyError, TypeError, ValueError) as error:
            raise Floor1CandidatePlanningError(
                "货位坐标无效，请刷新后重试", status_code=409
            ) from error
        if min(float(current[key]) for key in ("width_mm", "depth_mm")) <= 0:
            raise Floor1CandidatePlanningError("货位尺寸必须大于零", status_code=409)
        if not _rect_inside_polygon(
            x=float(current["x_mm"]),
            y=float(current["y_mm"]),
            width=float(current["width_mm"]),
            depth=float(current["depth_mm"]),
            polygon=polygon,
            epsilon_mm=geometry_epsilon,
        ) and not allow_spatial_conflicts:
            raise Floor1CandidatePlanningError(
                f"货位 {slot.get('location_id') or ''} 超出所属区域边界",
                status_code=409,
            )
        if (
            float(current["x_mm"]) < float(bounds["min_x"]) - geometry_epsilon
            or float(current["y_mm"]) < float(bounds["min_y"]) - geometry_epsilon
            or float(current["x_mm"]) + float(current["width_mm"]) > float(bounds["max_x"]) + geometry_epsilon
            or float(current["y_mm"]) + float(current["depth_mm"]) > float(bounds["max_y"]) + geometry_epsilon
        ):
            raise Floor1CandidatePlanningError(
                f"货位 {slot.get('location_id') or ''} 超出整层地图范围",
                status_code=409,
            )
        if not allow_spatial_conflicts and any(
            _rectangles_overlap(
                current,
                obstacle,
                epsilon_mm=geometry_epsilon,
            )
            for obstacle in obstacles
        ):
            raise Floor1CandidatePlanningError(
                f"货位 {slot.get('location_id') or ''} 与柱子、设备、货架或其他禁放设施冲突",
                status_code=409,
            )
        for previous in physical:
            if _rectangles_overlap(
                current,
                previous,
                epsilon_mm=geometry_epsilon,
            ) and not allow_spatial_conflicts:
                raise Floor1CandidatePlanningError(
                    "货位之间发生重叠，请拉开后再保存", status_code=409
                )
        physical.append(current)
    return physical


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
    combined_dispatch_features: list[dict] = []
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
        if subtype == "finished_wait_delivery":
            combined_dispatch_features.append(
                {
                    "map_feature_id": str(feature.get("id") or ""),
                    "feature_code": str(feature.get("feature_code") or ""),
                    "area_name": str(feature.get("name") or "一楼成品合并暂存区"),
                }
            )
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
            "combined_dispatch_features": combined_dispatch_features,
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
        "combined_dispatch_projection": {
            "location_code": "F1-DISPATCH-01",
            "location_name": "一楼成品合并暂存区",
            "map_feature_ids": [
                row["map_feature_id"] for row in combined_dispatch_features
            ],
            "creates_formal_locations": False,
        },
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
        if area is None or not _published_candidate_area_matches(
            area,
            candidate=candidate,
            map_revision=plan["map_revision"],
        ):
            return False
    return True


def _published_candidate_area_matches(
    area: WarehouseArea,
    *,
    candidate: dict,
    map_revision: str,
) -> bool:
    """Treat an administrator-published map binding as the current business fact.

    The automatic plan is only a default for regions that have not yet been
    configured.  Once an administrator has published the same measured zone,
    its chosen use, capacity and locations must not be overwritten by a later
    batch run.
    """

    policy = area.storage_policy
    return bool(
        policy is not None
        and area.construction_status == "enabled"
        and policy.status == "published"
        and policy.map_feature_id == candidate["map_feature_id"]
        and policy.published_map_revision == map_revision
    )


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
    candidate_by_code = {row["area_code"]: row for row in plan["candidates"]}
    resolved_candidate_areas = [
        area
        for area in candidate_areas
        if _published_candidate_area_matches(
            area,
            candidate=candidate_by_code[area.area_code],
            map_revision=plan["map_revision"],
        )
    ]
    resolved_candidate_area_ids = {area.id for area in resolved_candidate_areas}
    already_applied = len(resolved_candidate_areas) == len(plan["candidates"])
    blocking_items: list[dict] = []
    candidate_feature_ids = {
        row["area_code"]: row["map_feature_id"] for row in plan["candidates"]
    }
    if candidate_areas and not already_applied:
        for area in candidate_areas:
            if area.id in resolved_candidate_area_ids:
                continue
            blocking_items.append(
                {
                    "code": "partial_candidate_state",
                    "message": (
                        f"{area.area_code} 自动候选区域已存在部分正式台账或位置，"
                        "不能静默覆盖"
                    ),
                    "action_kind": "open_area_planning",
                    "action_label": "去核对区域设置",
                    "area_id": area.id,
                    "area_code": area.area_code,
                    "map_feature_id": candidate_feature_ids.get(area.area_code),
                    "location_id": None,
                    "live_lot_count": 0,
                    "current_pallet_count": 0,
                }
            )

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
        blocking_location_ids: list[int] = []
        if location_ids:
            live_lot_location_ids = set(
                db.scalars(
                    select(InventoryLot.warehouse_location_id)
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
            current_pallet_location_ids = set(
                db.scalars(
                    select(InventoryPallet.location_id)
                    .where(
                        InventoryPallet.location_id.in_(location_ids),
                        InventoryPallet.is_current.is_(True),
                    )
                    .distinct()
                ).all()
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
            blocking_location_ids = sorted(
                int(location_id)
                for location_id in live_lot_location_ids | current_pallet_location_ids
                if location_id is not None
            )
        active_location_count = sum(1 for row in locations if row.is_active)
        preserve_business_anchor = (
            area.area_code.upper() in _PRESERVED_BUSINESS_ANCHOR_AREA_CODES
        )
        has_blocker = bool(
            area.storage_policy is not None or live_lot_count or current_pallet_count
        )
        if area.storage_policy is not None and not preserve_business_anchor:
            blocking_items.append(
                {
                    "code": "legacy_area_policy",
                    "message": f"{area.area_code} 未映射台账区域仍绑定正式地图策略",
                    "action_kind": "open_area_planning",
                    "action_label": "去处理区域绑定",
                    "area_id": area.id,
                    "area_code": area.area_code,
                    "map_feature_id": area.storage_policy.map_feature_id,
                    "location_id": location_ids[0] if location_ids else None,
                    "live_lot_count": live_lot_count,
                    "current_pallet_count": current_pallet_count,
                }
            )
        if (live_lot_count or current_pallet_count) and not preserve_business_anchor:
            blocking_items.append(
                {
                    "code": "legacy_area_inventory",
                    "message": (
                        f"{area.area_code} 未映射台账区域仍有库存或实体栈板，不能自动停用"
                    ),
                    "action_kind": "open_inventory_move",
                    "action_label": "去移动库存和栈板",
                    "area_id": area.id,
                    "area_code": area.area_code,
                    "map_feature_id": (
                        area.storage_policy.map_feature_id
                        if area.storage_policy is not None
                        else None
                    ),
                    "location_id": (
                        blocking_location_ids[0] if blocking_location_ids else None
                    ),
                    "live_lot_count": live_lot_count,
                    "current_pallet_count": current_pallet_count,
                }
            )
        archive_required = not preserve_business_anchor and not has_blocker and bool(
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
                "preserve_business_anchor": preserve_business_anchor,
                "archive_required": archive_required,
                "action": (
                    "preserve_business_anchor"
                    if preserve_business_anchor
                    else "block"
                    if has_blocker
                    else "archive_empty_legacy"
                    if archive_required
                    else "keep_archived_history"
                ),
            }
        )

    blockers = [item["message"] for item in blocking_items]
    canonical = json.dumps(
        {
            "map_revision": plan["map_revision"],
            "already_applied": already_applied,
            "candidate_area_ids": [area.id for area in candidate_areas],
            "resolved_candidate_area_ids": sorted(resolved_candidate_area_ids),
            "legacy_areas": legacy_areas,
            "blocking_conflicts": blockers,
            "blocking_items": blocking_items,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "already_applied": already_applied,
        "resolved_candidate_area_count": len(resolved_candidate_areas),
        "archivable_legacy_area_count": sum(
            1 for row in legacy_areas if row["archive_required"]
        ),
        "legacy_areas": legacy_areas,
        "blocking_conflicts": blockers,
        "blocking_items": blocking_items,
    }


def _ensure_no_candidate_conflicts(
    db: Session,
    *,
    floor: WarehouseFloor,
    plan: dict,
) -> dict[str, WarehouseArea]:
    feature_ids = [row["map_feature_id"] for row in plan["candidates"]]
    area_codes = [row["area_code"] for row in plan["candidates"]]
    candidate_by_code = {row["area_code"]: row for row in plan["candidates"]}
    existing_areas = list(
        db.scalars(
            select(WarehouseArea)
            .options(selectinload(WarehouseArea.storage_policy))
            .where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code.in_(area_codes),
            )
        ).all()
    )
    existing_policies = list(
        db.scalars(
            select(WarehouseAreaStoragePolicy)
            .options(selectinload(WarehouseAreaStoragePolicy.area))
            .where(WarehouseAreaStoragePolicy.map_feature_id.in_(feature_ids))
        ).all()
    )
    resolved_by_code = {
        area.area_code: area
        for area in existing_areas
        if _published_candidate_area_matches(
            area,
            candidate=candidate_by_code[area.area_code],
            map_revision=plan["map_revision"],
        )
    }
    conflicting_areas = [
        area for area in existing_areas if area.area_code not in resolved_by_code
    ]
    conflicting_policies = [
        policy
        for policy in existing_policies
        if policy.area is None
        or policy.area.area_code not in resolved_by_code
        or resolved_by_code[policy.area.area_code].storage_policy is not policy
    ]
    if conflicting_areas or conflicting_policies:
        raise Floor1CandidatePlanningError(
            "候选区域已存在部分正式绑定，系统不会静默覆盖；请先核对冲突后再确认",
            status_code=409,
        )
    return resolved_by_code


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
                "未映射台账区域已变化，请刷新后重试",
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
        archive_note = "一楼实测区域确认时停用无货且未映射的重复台账记录"
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
    resolved_by_code = _ensure_no_candidate_conflicts(db, floor=floor, plan=plan)

    next_sort = int(db.scalar(select(func.max(WarehouseLocation.sort_order))) or 0) + 1
    areas: list[WarehouseArea] = []
    locations: list[WarehouseLocation] = []
    for candidate in plan["candidates"]:
        resolved_area = resolved_by_code.get(candidate["area_code"])
        if resolved_area is not None:
            areas.append(resolved_area)
            continue
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
                source_type="seeded",
                layout_kind=(
                    "logical_anchor"
                    if slot.get("capacity_confirmed") is True
                    else "physical_pallet"
                ),
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
    all_policies = list(db.scalars(policy_query).all())
    area_rows = list(
        db.scalars(
            select(WarehouseArea)
            .options(selectinload(WarehouseArea.storage_policy))
            .where(WarehouseArea.floor_id == floor.id)
        ).all()
    )
    tombstones = archived_area_tombstones_for_floor(
        db,
        floor_code=floor.floor_code,
    )
    floor_layout = filter_archived_area_layout(
        floor_layout,
        tombstones=tombstones,
    )
    policies = [
        policy
        for policy in all_policies
        if policy.status != "archived"
        and policy.area.construction_status != "archived"
        and (include_draft or policy.status == "published")
    ]
    active_area_rows = [
        area
        for area in area_rows
        if area.construction_status != "archived"
        and (
            area.storage_policy is None
            or area.storage_policy.status != "archived"
        )
    ]
    areas_by_code = {area.area_code.upper(): area for area in active_area_rows}
    areas_by_id = {area.id: area for area in areas_by_code.values()}
    feature_area_code_counts: dict[str, int] = {}
    for raw in floor_layout.get("features") or []:
        if raw.get("feature_kind") != "zone":
            continue
        area_code = str(raw.get("erp_area_code") or "").strip().upper()
        if area_code:
            feature_area_code_counts[area_code] = (
                feature_area_code_counts.get(area_code, 0) + 1
            )
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
            feature["formal_policy_version"] = policy.version
            feature["formal_published_map_revision"] = (
                policy.published_map_revision
            )
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
            feature["area_master_name"] = policy.area.area_name
            feature["employee_area_name"] = employee_area_name(
                feature,
                area_code=policy.area.area_code,
                floor_number=floor.floor_number,
            )
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
        elif str(feature.get("erp_area_code") or "").strip():
            # A measured zone and a formal area are the same operational area
            # when their identity is proven by one unique same-floor code and
            # active formal locations.  This projection is evaluated before
            # draft display state so an unrelated layout draft can never make
            # an enabled area look disabled.
            legacy_code = str(feature.get("erp_area_code") or "").strip().upper()
            legacy_area = areas_by_code.get(legacy_code)
            direct_binding_is_proven = floor3_v11_map_binding_is_proven(
                db,
                floor=floor,
                feature=feature,
                area=legacy_area,
                feature_area_code_count=feature_area_code_counts.get(legacy_code, 0),
            )
            if direct_binding_is_proven and legacy_area is not None:
                legacy_policy = legacy_v11_area_policy_projection(
                    db,
                    floor=floor,
                    area=legacy_area,
                )
                feature["formal_area_id"] = legacy_area.id
                feature["formal_floor_id"] = legacy_area.floor_id
                feature["formal_binding_source"] = "formal_area_code"
                feature["formal_binding_status"] = (
                    "published"
                    if legacy_area.construction_status == "enabled"
                    else "inactive"
                )
                if include_draft:
                    feature.setdefault("formal_area_name", legacy_area.area_name)
                else:
                    feature["formal_area_name"] = legacy_area.area_name
                feature["area_master_name"] = legacy_area.area_name
                feature["employee_area_name"] = employee_area_name(
                    feature,
                    area_code=legacy_area.area_code,
                    floor_number=floor.floor_number,
                )
                if legacy_policy is not None:
                    legacy_inventory_types, legacy_storage_layout = legacy_policy
                    feature.setdefault(
                        "allowed_inventory_types", legacy_inventory_types
                    )
                    feature.setdefault("storage_layout", legacy_storage_layout)
                feature["formal_construction_status"] = legacy_area.construction_status
                feature["planned_location_count"] = legacy_area.planned_location_count
                feature["planned_pallet_capacity"] = legacy_area.planned_pallet_capacity
                feature["capacity_review_status"] = legacy_area.capacity_review_status
                feature["capacity_eligible"] = legacy_area.capacity_eligible
                feature["confirmed_pallet_capacity"] = (
                    legacy_area.confirmed_pallet_capacity
                )
                feature["capacity_reviewed_by"] = legacy_area.capacity_reviewed_by
                feature["capacity_reviewed_at"] = (
                    legacy_area.capacity_reviewed_at.isoformat()
                    if legacy_area.capacity_reviewed_at
                    else None
                )
            elif include_draft and has_draft:
                feature["formal_binding_status"] = "draft"
                draft_area_id = feature.get("formal_area_id")
                draft_area = (
                    areas_by_id.get(int(draft_area_id))
                    if isinstance(draft_area_id, int) or str(draft_area_id or "").isdigit()
                    else None
                )
                if draft_area is not None:
                    if (
                        draft_area.floor_id != floor.id
                        or draft_area.area_code.upper() != legacy_code
                    ):
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

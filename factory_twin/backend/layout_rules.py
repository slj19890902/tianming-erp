from __future__ import annotations

from math import hypot

from .models import EquipmentPlacement, Layout, LayoutFeature, RackPlacement


RULE_DEFAULTS_MM = {
    "pedestrian": 1200.0,
    "forklift": 3000.0,
    "shared_main": 1900.0,
    "shared_secondary": 1500.0,
    "fire": 4000.0,
    "loading": 3500.0,
}


def polygon_area_mm2(points: list[list[float]]) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += float(x1) * float(y2) - float(x2) * float(y1)
    return round(abs(area) / 2, 3)


def polyline_length_mm(points: list[list[float]]) -> float:
    return round(
        sum(
            hypot(float(x2) - float(x1), float(y2) - float(y1))
            for (x1, y1), (x2, y2) in zip(points, points[1:])
        ),
        3,
    )


def feature_area_mm2(feature_kind: str, points: list[list[float]], width_mm: float | None) -> float:
    if feature_kind in {"aisle", "structure"}:
        return round(polyline_length_mm(points) * float(width_mm or 0), 3)
    return polygon_area_mm2(points)


def _rect_bounds(
    x: float, y: float, width: float, depth: float, rotation: int
) -> tuple[float, float, float, float]:
    if rotation % 180 == 90:
        width, depth = depth, width
    return (x - width / 2, y - depth / 2, x + width / 2, y + depth / 2)


def _points_bounds(points: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bounds_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] <= second[0]
        or first[0] >= second[2]
        or first[3] <= second[1]
        or first[1] >= second[3]
    )


def _point_in_polygon(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    epsilon = 1e-9

    def on_segment(
        start: tuple[float, float],
        point: tuple[float, float],
        end: tuple[float, float],
    ) -> bool:
        return (
            min(start[0], end[0]) - epsilon <= point[0] <= max(start[0], end[0]) + epsilon
            and min(start[1], end[1]) - epsilon <= point[1] <= max(start[1], end[1]) + epsilon
        )

    first_a = _orientation(first_start, first_end, second_start)
    first_b = _orientation(first_start, first_end, second_end)
    second_a = _orientation(second_start, second_end, first_start)
    second_b = _orientation(second_start, second_end, first_end)
    if first_a * first_b < -epsilon and second_a * second_b < -epsilon:
        return True
    return (
        (abs(first_a) <= epsilon and on_segment(first_start, second_start, first_end))
        or (abs(first_b) <= epsilon and on_segment(first_start, second_end, first_end))
        or (abs(second_a) <= epsilon and on_segment(second_start, first_start, second_end))
        or (abs(second_b) <= epsilon and on_segment(second_start, first_end, second_end))
    )


def _polygon_hits_rect(
    polygon: list[list[float]], rect: tuple[float, float, float, float]
) -> bool:
    if not _bounds_intersect(_points_bounds(polygon), rect):
        return False
    corners = [
        (rect[0], rect[1]),
        (rect[2], rect[1]),
        (rect[2], rect[3]),
        (rect[0], rect[3]),
    ]
    if any(rect[0] <= float(x) <= rect[2] and rect[1] <= float(y) <= rect[3] for x, y in polygon):
        return True
    if any(_point_in_polygon(corner, polygon) for corner in corners):
        return True
    polygon_edges = [
        ((float(start[0]), float(start[1])), (float(end[0]), float(end[1])))
        for start, end in zip(polygon, polygon[1:] + polygon[:1])
    ]
    rect_edges = list(zip(corners, corners[1:] + corners[:1]))
    return any(
        _segments_intersect(poly_start, poly_end, rect_start, rect_end)
        for poly_start, poly_end in polygon_edges
        for rect_start, rect_end in rect_edges
    )


def _segment_bounds(
    start: list[float], end: list[float], padding: float
) -> tuple[float, float, float, float]:
    return (
        min(float(start[0]), float(end[0])) - padding,
        min(float(start[1]), float(end[1])) - padding,
        max(float(start[0]), float(end[0])) + padding,
        max(float(start[1]), float(end[1])) + padding,
    )


def _feature_hits_rect(
    feature: LayoutFeature, rect: tuple[float, float, float, float]
) -> bool:
    points = feature.points_json
    if feature.feature_kind == "aisle":
        padding = float(feature.width_mm or 0) / 2
        return any(
            _bounds_intersect(_segment_bounds(start, end, padding), rect)
            for start, end in zip(points, points[1:])
        )
    return _polygon_hits_rect(points, rect)


def _vertical_intervals_overlap(
    first_low: float, first_high: float, second_low: float, second_high: float
) -> bool:
    return first_low < second_high and first_high > second_low


def _structure_rect(structure: dict) -> tuple[float, float, float, float] | None:
    geometry = structure.get("geometry", {})
    if geometry.get("type") == "polyline" and geometry.get("points"):
        return _points_bounds(geometry["points"])
    if geometry.get("type") == "circle":
        radius = float(geometry.get("radius_mm") or 0)
        x = float(geometry.get("x_mm") or 0)
        y = float(geometry.get("y_mm") or 0)
        return x - radius, y - radius, x + radius, y + radius
    return None


def evaluate_layout_rules(layout: Layout) -> list[dict]:
    violations: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()

    def add(
        *,
        rule_code: str,
        message: str,
        entity_kind: str,
        entity_id: str,
        related_kind: str | None = None,
        related_id: str | None = None,
        severity: str = "error",
    ) -> None:
        key = (rule_code, entity_id, related_id)
        if key in seen:
            return
        seen.add(key)
        violations.append(
            {
                "id": f"{rule_code}:{entity_id}:{related_id or '-'}",
                "severity": severity,
                "rule_code": rule_code,
                "message": message,
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "related_kind": related_kind,
                "related_id": related_id,
            }
        )

    equipment_rects = {
        item.id: _rect_bounds(
            item.x_mm, item.y_mm, item.width_mm, item.depth_mm, item.rotation_deg
        )
        for item in layout.placements
    }
    rack_rects = {
        item.id: _rect_bounds(
            item.x_mm, item.y_mm, item.width_mm, item.depth_mm, item.rotation_deg
        )
        for item in layout.racks
    }
    column_rects = {
        item["id"]: rect
        for item in layout.structures_json
        if item.get("kind") == "column"
        if (rect := _structure_rect(item)) is not None
    }
    aisles = [item for item in layout.features if item.feature_kind == "aisle"]
    zones = [item for item in layout.features if item.feature_kind == "zone"]
    no_go_areas = [item for item in layout.features if item.feature_kind == "no_go"]

    for aisle in aisles:
        minimum = RULE_DEFAULTS_MM.get(aisle.subtype)
        if minimum and float(aisle.width_mm or 0) < minimum:
            add(
                rule_code="AISLE_WIDTH_LOW",
                message=(
                    f"{aisle.name}宽度 {aisle.width_mm:g}mm，低于当前建议值 {minimum:g}mm；"
                    "建议值不是现场已确认标准。"
                ),
                entity_kind="feature",
                entity_id=aisle.id,
                severity="warning",
            )
        for rack in layout.racks:
            if _feature_hits_rect(aisle, rack_rects[rack.id]):
                add(
                    rule_code="RACK_IN_AISLE",
                    message=f"货架 {rack.rack_code} 占用通道 {aisle.feature_code}。",
                    entity_kind="rack",
                    entity_id=rack.id,
                    related_kind="feature",
                    related_id=aisle.id,
                )

    for zone in zones:
        for equipment in layout.placements:
            if _feature_hits_rect(zone, equipment_rects[equipment.id]):
                if zone.storage_mode == "floor":
                    add(
                        rule_code="ZONE_OVER_EQUIPMENT",
                        message=f"地面堆放区 {zone.feature_code} 压到设备 {equipment.name}。",
                        entity_kind="feature",
                        entity_id=zone.id,
                        related_kind="equipment",
                        related_id=equipment.id,
                    )
                elif _vertical_intervals_overlap(
                    zone.elevation_mm,
                    zone.elevation_mm + zone.storage_height_mm,
                    equipment.z_mm,
                    equipment.z_mm + equipment.height_mm,
                ):
                    add(
                        rule_code="ZONE_VERTICAL_OVER_EQUIPMENT",
                        message=(
                            f"上层区域 {zone.feature_code} 与设备 {equipment.name} 二维重叠，"
                            f"且垂直高度区间仍相交；请核对离地高度。"
                        ),
                        entity_kind="feature",
                        entity_id=zone.id,
                        related_kind="equipment",
                        related_id=equipment.id,
                    )
        for column_id, rect in column_rects.items():
            if _feature_hits_rect(zone, rect):
                add(
                    rule_code="ZONE_OVER_COLUMN",
                    message=f"堆放区 {zone.feature_code} 压到固定柱子。",
                    entity_kind="feature",
                    entity_id=zone.id,
                    related_kind="structure",
                    related_id=column_id,
                )

    for no_go in no_go_areas:
        label = "消防出口" if no_go.subtype == "fire_exit" else no_go.name
        for rack in layout.racks:
            if _feature_hits_rect(no_go, rack_rects[rack.id]):
                add(
                    rule_code="NO_GO_BLOCKED",
                    message=f"{label}被货架 {rack.rack_code} 占用。",
                    entity_kind="rack",
                    entity_id=rack.id,
                    related_kind="feature",
                    related_id=no_go.id,
                )
        for equipment in layout.placements:
            if _feature_hits_rect(no_go, equipment_rects[equipment.id]):
                add(
                    rule_code="NO_GO_BLOCKED",
                    message=f"{label}被设备 {equipment.name} 占用。",
                    entity_kind="equipment",
                    entity_id=equipment.id,
                    related_kind="feature",
                    related_id=no_go.id,
                )
        for zone in zones:
            if _bounds_intersect(_points_bounds(no_go.points_json), _points_bounds(zone.points_json)):
                add(
                    rule_code="NO_GO_BLOCKED",
                    message=f"{label}被堆放区 {zone.feature_code} 遮挡。",
                    entity_kind="feature",
                    entity_id=zone.id,
                    related_kind="feature",
                    related_id=no_go.id,
                )

    return sorted(violations, key=lambda item: (item["severity"], item["rule_code"], item["id"]))

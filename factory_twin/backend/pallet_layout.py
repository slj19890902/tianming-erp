from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from .layout_rules import (
    _bounds_intersect,
    _feature_hits_rect,
    _point_in_polygon,
    _points_bounds,
    _rect_bounds,
    _segment_bounds,
    _structure_rect,
)
from .models import Layout, LayoutFeature, PalletPlacement


@dataclass(frozen=True)
class PalletPosition:
    x_mm: float
    y_mm: float
    zone: LayoutFeature
    snapped: bool


def _point_on_segment(point: tuple[float, float], start: list[float], end: list[float]) -> bool:
    x, y = point
    x1, y1 = float(start[0]), float(start[1])
    x2, y2 = float(end[0]), float(end[1])
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-6:
        return False
    return min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6 and min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6


def _point_in_or_on_polygon(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    if any(_point_on_segment(point, start, end) for start, end in zip(polygon, polygon[1:] + polygon[:1])):
        return True
    return _point_in_polygon(point, polygon)


def _rect_sample_points(rect: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    left, bottom, right, top = rect
    center_x = (left + right) / 2
    center_y = (bottom + top) / 2
    return [
        (left, bottom), (center_x, bottom), (right, bottom),
        (left, center_y), (center_x, center_y), (right, center_y),
        (left, top), (center_x, top), (right, top),
    ]


def _storage_zone_for_rect(layout: Layout, rect: tuple[float, float, float, float]) -> LayoutFeature | None:
    for zone in layout.features:
        if zone.feature_kind != "zone" or zone.storage_mode != "floor" or len(zone.points_json) < 3:
            continue
        if all(_point_in_or_on_polygon(point, zone.points_json) for point in _rect_sample_points(rect)):
            return zone
    return None


def _structure_feature_hits_rect(feature: LayoutFeature, rect: tuple[float, float, float, float]) -> bool:
    padding = float(feature.width_mm or 0) / 2
    return any(
        _bounds_intersect(_segment_bounds(start, end, padding), rect)
        for start, end in zip(feature.points_json, feature.points_json[1:])
    )


def validate_pallet_position(
    layout: Layout,
    *,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    depth_mm: float,
    rotation_deg: int,
    exclude_pallet_id: str | None = None,
) -> LayoutFeature:
    rect = _rect_bounds(x_mm, y_mm, width_mm, depth_mm, rotation_deg)
    zone = _storage_zone_for_rect(layout, rect)
    if zone is None:
        raise ValueError("栈板必须完整放在地面可存放区域内")

    for pallet in layout.pallets:
        if pallet.id == exclude_pallet_id:
            continue
        other = _rect_bounds(pallet.x_mm, pallet.y_mm, pallet.width_mm, pallet.depth_mm, pallet.rotation_deg)
        if _bounds_intersect(rect, other):
            raise ValueError(f"栈板与 {pallet.pallet_code} 重叠")

    for placement in layout.placements:
        obstacle = _rect_bounds(
            placement.x_mm, placement.y_mm, placement.width_mm, placement.depth_mm, placement.rotation_deg
        )
        if _bounds_intersect(rect, obstacle):
            raise ValueError(f"栈板压到设备 {placement.name}")

    for feature in layout.features:
        if feature.feature_kind in {"aisle", "no_go"} and _feature_hits_rect(feature, rect):
            label = "通道" if feature.feature_kind == "aisle" else "禁放区"
            raise ValueError(f"栈板压到{label} {feature.feature_code}")
        if feature.feature_kind == "structure" and feature.subtype in {"custom_wall", "custom_column", "freight_elevator"}:
            if _structure_feature_hits_rect(feature, rect):
                raise ValueError(f"栈板压到人工结构 {feature.feature_code}")

    for structure in layout.structures_json:
        if structure.get("kind") != "column":
            continue
        obstacle = _structure_rect(structure)
        if obstacle is not None and _bounds_intersect(rect, obstacle):
            raise ValueError("栈板压到固定柱子")
    return zone


def place_pallet(
    layout: Layout,
    *,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    depth_mm: float,
    rotation_deg: int,
    snap_enabled: bool,
    snap_threshold_mm: float,
    exclude_pallet_id: str | None = None,
) -> PalletPosition:
    actual_width, actual_depth = (depth_mm, width_mm) if rotation_deg % 180 == 90 else (width_mm, depth_mm)
    half_width, half_depth = actual_width / 2, actual_depth / 2
    x_targets: set[float] = set()
    y_targets: set[float] = set()

    if snap_enabled and snap_threshold_mm > 0:
        for zone in layout.features:
            if zone.feature_kind != "zone" or zone.storage_mode != "floor" or len(zone.points_json) < 3:
                continue
            left, bottom, right, top = _points_bounds(zone.points_json)
            x_targets.update({left + half_width, (left + right) / 2, right - half_width})
            y_targets.update({bottom + half_depth, (bottom + top) / 2, top - half_depth})
        for pallet in layout.pallets:
            if pallet.id == exclude_pallet_id:
                continue
            left, bottom, right, top = _rect_bounds(
                pallet.x_mm, pallet.y_mm, pallet.width_mm, pallet.depth_mm, pallet.rotation_deg
            )
            x_targets.update({left - half_width, left + half_width, pallet.x_mm, right - half_width, right + half_width})
            y_targets.update({bottom - half_depth, bottom + half_depth, pallet.y_mm, top - half_depth, top + half_depth})

        nearby_x = sorted((value for value in x_targets if abs(value - x_mm) <= snap_threshold_mm), key=lambda value: abs(value - x_mm))[:8]
        nearby_y = sorted((value for value in y_targets if abs(value - y_mm) <= snap_threshold_mm), key=lambda value: abs(value - y_mm))[:8]
        candidates: list[tuple[float, float, float]] = []
        for candidate_x, candidate_y in product([x_mm, *nearby_x], [y_mm, *nearby_y]):
            if candidate_x == x_mm and candidate_y == y_mm:
                continue
            distance = abs(candidate_x - x_mm) + abs(candidate_y - y_mm)
            candidates.append((distance, candidate_x, candidate_y))
        for _, candidate_x, candidate_y in sorted(candidates):
            try:
                zone = validate_pallet_position(
                    layout,
                    x_mm=candidate_x,
                    y_mm=candidate_y,
                    width_mm=width_mm,
                    depth_mm=depth_mm,
                    rotation_deg=rotation_deg,
                    exclude_pallet_id=exclude_pallet_id,
                )
                return PalletPosition(candidate_x, candidate_y, zone, True)
            except ValueError:
                continue

    zone = validate_pallet_position(
        layout,
        x_mm=x_mm,
        y_mm=y_mm,
        width_mm=width_mm,
        depth_mm=depth_mm,
        rotation_deg=rotation_deg,
        exclude_pallet_id=exclude_pallet_id,
    )
    return PalletPosition(x_mm, y_mm, zone, False)


def evaluate_pallet_rules(layout: Layout) -> list[dict]:
    violations: list[dict] = []
    for pallet in layout.pallets:
        try:
            validate_pallet_position(
                layout,
                x_mm=pallet.x_mm,
                y_mm=pallet.y_mm,
                width_mm=pallet.width_mm,
                depth_mm=pallet.depth_mm,
                rotation_deg=pallet.rotation_deg,
                exclude_pallet_id=pallet.id,
            )
        except ValueError as error:
            violations.append({
                "id": f"PALLET_POSITION_INVALID:{pallet.id}:-",
                "severity": "error",
                "rule_code": "PALLET_POSITION_INVALID",
                "message": f"栈板 {pallet.pallet_code}：{error}",
                "entity_kind": "pallet",
                "entity_id": pallet.id,
                "related_kind": None,
                "related_id": None,
            })
    return violations

"""Align the candidate 1F twin layout to the authoritative measured 3F frame.

The operator first aligns the 3F-left reference over 1F in the browser.  That
visible draft describes a transform from 3F coordinates to the legacy 1F
coordinates.  This tool applies its inverse to 1F and then uses the orthogonal
3F column grid as independent X/Y control lines.  The source DXF is never
modified.  ``--apply`` updates only the candidate twin SQLite database after a
verified SQLite backup and a complete JSON layout snapshot have been written.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from factory_twin.backend.database import create_database_engine, create_session_factory
from factory_twin.backend.models import (
    EquipmentPlacement,
    Layout,
    LayoutFeature,
    PalletPlacement,
    RackPlacement,
)


COORDINATE_FRAME_MARKER = "COORDINATE_FRAME:3F_MEASURED_V1"
LEGACY_BASE = {
    "scale_x": 0.88795,
    "scale_y": 1.09448,
    "offset_x_mm": -15111.0,
    "offset_y_mm": -11747.0,
}


@dataclass(frozen=True)
class Calibration:
    offset_x_mm: float
    offset_y_mm: float
    scale_x: float
    scale_y: float
    mirror_x: bool
    mirror_y: bool
    rotation_deg: float


@dataclass(frozen=True)
class AxisControl:
    source_mm: float
    target_mm: float


def _anchor(bounds: dict[str, float]) -> tuple[float, float]:
    middle_x = (bounds["min_x"] + bounds["max_x"]) / 2
    return (
        (bounds["min_x"] + middle_x) / 2,
        (bounds["min_y"] + bounds["max_y"]) / 2,
    )


def reference_point_to_legacy_1f(
    point: tuple[float, float],
    bounds_3f: dict[str, float],
    calibration: Calibration,
) -> tuple[float, float]:
    """Mirror the browser reference-overlay transform (3F -> legacy 1F)."""

    anchor_x, anchor_y = _anchor(bounds_3f)
    base_x = -anchor_x * LEGACY_BASE["scale_x"] + LEGACY_BASE["offset_x_mm"]
    base_y = -anchor_y * LEGACY_BASE["scale_y"] + LEGACY_BASE["offset_y_mm"]
    scaled_x = (-1 if calibration.mirror_x else 1) * (point[0] - anchor_x) * calibration.scale_x
    scaled_y = (-1 if calibration.mirror_y else 1) * (point[1] - anchor_y) * calibration.scale_y
    angle = math.radians(calibration.rotation_deg)
    return (
        base_x + scaled_x * math.cos(angle) - scaled_y * math.sin(angle) + calibration.offset_x_mm,
        base_y + scaled_x * math.sin(angle) + scaled_y * math.cos(angle) + calibration.offset_y_mm,
    )


def legacy_1f_point_to_reference(
    point: tuple[float, float],
    bounds_3f: dict[str, float],
    calibration: Calibration,
) -> tuple[float, float]:
    """Invert the saved browser transform (legacy 1F -> measured 3F)."""

    if abs(calibration.scale_x) < 1e-9 or abs(calibration.scale_y) < 1e-9:
        raise ValueError("reference scale cannot be zero")
    anchor_x, anchor_y = _anchor(bounds_3f)
    base_x = -anchor_x * LEGACY_BASE["scale_x"] + LEGACY_BASE["offset_x_mm"]
    base_y = -anchor_y * LEGACY_BASE["scale_y"] + LEGACY_BASE["offset_y_mm"]
    shifted_x = point[0] - base_x - calibration.offset_x_mm
    shifted_y = point[1] - base_y - calibration.offset_y_mm
    angle = math.radians(calibration.rotation_deg)
    unrotated_x = shifted_x * math.cos(angle) + shifted_y * math.sin(angle)
    unrotated_y = -shifted_x * math.sin(angle) + shifted_y * math.cos(angle)
    sign_x = -1 if calibration.mirror_x else 1
    sign_y = -1 if calibration.mirror_y else 1
    return (
        anchor_x + unrotated_x / (sign_x * calibration.scale_x),
        anchor_y + unrotated_y / (sign_y * calibration.scale_y),
    )


def _points_center(points: list[list[float]]) -> tuple[float, float]:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def _structure_center(geometry: dict[str, Any]) -> tuple[float, float]:
    if geometry.get("points"):
        return _points_center(geometry["points"])
    return float(geometry.get("x_mm", 0)), float(geometry.get("y_mm", 0))


def _column_centers(layout: Layout) -> list[tuple[str, tuple[float, float]]]:
    columns: list[tuple[str, tuple[float, float]]] = []
    for structure in layout.structures_json:
        if structure.get("kind") == "column":
            columns.append((
                structure.get("column_code") or structure.get("source_handle") or "column",
                _structure_center(structure["geometry"]),
            ))
    for feature in layout.features:
        if feature.subtype == "custom_column":
            columns.append((feature.feature_code, _points_center(feature.points_json)))
    return columns


def _lift_features(layout: Layout) -> list[LayoutFeature]:
    return [
        feature for feature in layout.features
        if feature.subtype == "freight_elevator" and feature.feature_code.upper().startswith("LIFT-")
    ]


def _axis_controls(
    matches: list[dict[str, Any]],
    axis: int,
) -> list[AxisControl]:
    grouped: dict[float, list[float]] = {}
    for row in matches:
        target = round(float(row["target_mm"][axis]), 3)
        grouped.setdefault(target, []).append(float(row["global_mm"][axis]))
    controls = [
        AxisControl(source_mm=sum(values) / len(values), target_mm=target)
        for target, values in grouped.items()
    ]
    controls.sort(key=lambda item: item.source_mm)
    if len(controls) < 2:
        raise RuntimeError("column grid needs at least two control lines per axis")
    if any(right.source_mm <= left.source_mm for left, right in zip(controls, controls[1:])):
        raise RuntimeError("source column controls are not strictly increasing")
    if any(right.target_mm <= left.target_mm for left, right in zip(controls, controls[1:])):
        raise RuntimeError("target column controls would mirror or fold the layout")
    return controls


def warp_axis(value: float, controls: list[AxisControl]) -> float:
    """Piecewise-linear, monotonic interpolation with end-segment extrapolation."""

    if len(controls) < 2:
        raise ValueError("at least two controls are required")
    if value <= controls[0].source_mm:
        left, right = controls[0], controls[1]
    elif value >= controls[-1].source_mm:
        left, right = controls[-2], controls[-1]
    else:
        left, right = next(
            (left, right)
            for left, right in zip(controls, controls[1:])
            if left.source_mm <= value <= right.source_mm
        )
    ratio = (value - left.source_mm) / (right.source_mm - left.source_mm)
    return left.target_mm + ratio * (right.target_mm - left.target_mm)


def build_alignment_plan(
    one: Layout,
    three: Layout,
    calibration: Calibration,
    *,
    column_tolerance_mm: float = 1000.0,
) -> dict[str, Any]:
    if COORDINATE_FRAME_MARKER in one.warnings_json:
        return {
            "safe_to_apply": False,
            "already_aligned": True,
            "blockers": ["1F candidate already carries the measured 3F coordinate-frame marker."],
        }
    global_transform = lambda point: legacy_1f_point_to_reference(point, three.bounds_json, calibration)
    columns_1f = _column_centers(one)
    columns_3f = _column_centers(three)
    if not columns_1f or not columns_3f:
        raise RuntimeError("both layouts require column reference points")
    matches: list[dict[str, Any]] = []
    claimed_targets: set[str] = set()
    duplicate_targets: list[str] = []
    for code, source_point in columns_1f:
        global_point = global_transform(source_point)
        target_code, target_point = min(
            columns_3f,
            key=lambda item: math.dist(global_point, item[1]),
        )
        if target_code in claimed_targets:
            duplicate_targets.append(target_code)
        claimed_targets.add(target_code)
        matches.append({
            "column_1f": code,
            "source_mm": list(source_point),
            "global_mm": list(global_point),
            "column_3f": target_code,
            "target_mm": list(target_point),
            "distance_before_grid_mm": math.dist(global_point, target_point),
        })
    x_controls = _axis_controls(matches, 0)
    y_controls = _axis_controls(matches, 1)

    def align_point(point: tuple[float, float]) -> tuple[float, float]:
        global_point = global_transform(point)
        return warp_axis(global_point[0], x_controls), warp_axis(global_point[1], y_controls)

    for row in matches:
        aligned = (
            warp_axis(row["global_mm"][0], x_controls),
            warp_axis(row["global_mm"][1], y_controls),
        )
        row["aligned_mm"] = list(aligned)
        row["distance_after_grid_mm"] = math.dist(aligned, row["target_mm"])

    lift_report: dict[str, Any] | None = None
    one_lifts = _lift_features(one)
    three_lifts = _lift_features(three)
    if len(one_lifts) == 1 and len(three_lifts) == 1:
        source_lift = _points_center(one_lifts[0].points_json)
        target_lift = _points_center(three_lifts[0].points_json)
        aligned_lift = align_point(source_lift)
        lift_report = {
            "one_code": one_lifts[0].feature_code,
            "three_code": three_lifts[0].feature_code,
            "codes_match": one_lifts[0].feature_code == three_lifts[0].feature_code,
            "aligned_1f_center_mm": list(aligned_lift),
            "three_center_mm": list(target_lift),
            "distance_mm": math.dist(aligned_lift, target_lift),
            "snapped": False,
            "note": "The lift is reported but does not distort the authoritative column grid.",
        }
    distances_before = [row["distance_before_grid_mm"] for row in matches]
    distances_after = [row["distance_after_grid_mm"] for row in matches]
    calibration_safe = (
        not calibration.mirror_x
        and not calibration.mirror_y
        and abs(calibration.scale_x - 1) <= 0.001
        and abs(calibration.scale_y - 1) <= 0.001
        and abs(calibration.rotation_deg + 90) <= 0.001
    )
    blockers = []
    if duplicate_targets:
        blockers.append(f"Column matches are not one-to-one: {sorted(set(duplicate_targets))}")
    if max(distances_before) > column_tolerance_mm:
        blockers.append("At least one manually aligned 1F column is farther than the allowed tolerance from the 3F grid.")
    if not calibration_safe:
        blockers.append("The accepted browser draft must be -90 degrees, scale 1/1, with no mirrors.")
    if len(x_controls) < 3 or len(y_controls) < 3:
        blockers.append("The overlap does not expose at least three orthogonal column control lines per axis.")
    return {
        "mode": "candidate_dry_run",
        "authoritative_floor": "3F",
        "source_dxf_changed": False,
        "one_floor_layout_id": one.id,
        "three_floor_layout_id": three.id,
        "browser_calibration": calibration.__dict__,
        "global_inverse_rotation_deg": -calibration.rotation_deg,
        "x_controls": [control.__dict__ for control in x_controls],
        "y_controls": [control.__dict__ for control in y_controls],
        "column_count": len(matches),
        "column_distance_before_grid_mm": {
            "minimum": min(distances_before),
            "mean": sum(distances_before) / len(distances_before),
            "maximum": max(distances_before),
        },
        "column_distance_after_grid_mm": {
            "minimum": min(distances_after),
            "mean": sum(distances_after) / len(distances_after),
            "maximum": max(distances_after),
        },
        "lift_review": lift_report,
        "matches": matches,
        "safe_to_apply": not blockers,
        "blockers": blockers,
        "align_point": align_point,
    }


def _polygon_area(points: list[list[float]]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) / 2


def _transform_geometry(
    geometry: dict[str, Any],
    align_point: Callable[[tuple[float, float]], tuple[float, float]],
    coordinate_rotation_deg: float,
) -> dict[str, Any]:
    transformed = copy.deepcopy(geometry)
    if transformed.get("points"):
        transformed["points"] = [list(align_point((float(x), float(y)))) for x, y in transformed["points"]]
    if "x_mm" in transformed and "y_mm" in transformed:
        transformed["x_mm"], transformed["y_mm"] = align_point((
            float(transformed["x_mm"]), float(transformed["y_mm"]),
        ))
    if "rotation_deg" in transformed:
        transformed["rotation_deg"] = (float(transformed["rotation_deg"]) + coordinate_rotation_deg) % 360
    return transformed


def _rotate_clockwise_storage_angle(value: int, coordinate_rotation_deg: float) -> int:
    return int(round((float(value) - coordinate_rotation_deg) % 360))


def _rotate_access_side(side: str, coordinate_rotation_deg: float) -> str:
    if side == "both":
        return side
    vectors = {"east": (1.0, 0.0), "north": (0.0, 1.0), "west": (-1.0, 0.0), "south": (0.0, -1.0)}
    vector = vectors.get(side)
    if vector is None:
        return side
    angle = math.radians(coordinate_rotation_deg)
    rotated = (
        vector[0] * math.cos(angle) - vector[1] * math.sin(angle),
        vector[0] * math.sin(angle) + vector[1] * math.cos(angle),
    )
    return min(vectors, key=lambda key: math.dist(rotated, vectors[key]))


def _layout_snapshot(layout: Layout) -> dict[str, Any]:
    return {
        "layout": {
            "id": layout.id,
            "name": layout.name,
            "floor_code": layout.floor_code,
            "source_name": layout.source_name,
            "source_sha256": layout.source_sha256,
            "source_units": layout.source_units,
            "bounds_mm": layout.bounds_json,
            "structures": layout.structures_json,
            "warnings": layout.warnings_json,
        },
        "placements": [{
            key: getattr(row, key) for key in (
                "id", "template_id", "name", "x_mm", "y_mm", "z_mm", "width_mm", "depth_mm",
                "height_mm", "rotation_deg", "is_confirmed", "is_locked", "version",
            )
        } for row in layout.placements],
        "racks": [{
            key: getattr(row, key) for key in (
                "id", "rack_code", "name", "x_mm", "y_mm", "z_mm", "width_mm", "depth_mm",
                "height_mm", "levels", "cargo_rows", "bays", "access_side", "min_aisle_width_mm",
                "rotation_deg", "color", "source", "status", "is_locked", "version",
            )
        } | {"level_heights_mm": row.level_heights_json} for row in layout.racks],
        "pallets": [{
            key: getattr(row, key) for key in (
                "id", "pallet_code", "name", "zone_id", "x_mm", "y_mm", "z_mm", "width_mm",
                "depth_mm", "height_mm", "rotation_deg", "color", "version",
            )
        } for row in layout.pallets],
        "features": [{
            key: getattr(row, key) for key in (
                "id", "feature_code", "name", "feature_kind", "subtype", "width_mm", "direction",
                "no_stacking", "storage_mode", "elevation_mm", "storage_height_mm", "color", "area_mm2",
                "source", "status", "version",
            )
        } | {"points": row.points_json} for row in layout.features],
    }


def _verified_sqlite_backup(database: Path, backup: Path) -> dict[str, Any]:
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
        quick_check = target.execute("PRAGMA quick_check").fetchone()[0]
        source_tables = source.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        backup_tables = target.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if quick_check != "ok" or source_tables != backup_tables:
            raise RuntimeError("SQLite backup verification failed")
        return {"quick_check": quick_check, "table_count": backup_tables, "size_bytes": backup.stat().st_size}
    finally:
        source.close()
        target.close()


def apply_alignment(layout: Layout, plan: dict[str, Any]) -> None:
    if not plan.get("safe_to_apply"):
        raise RuntimeError("alignment plan is not safe to apply")
    align_point = plan["align_point"]
    coordinate_rotation_deg = float(plan["global_inverse_rotation_deg"])
    layout.structures_json = [
        {**structure, "geometry": _transform_geometry(structure["geometry"], align_point, coordinate_rotation_deg)}
        for structure in layout.structures_json
    ]
    old_bounds = layout.bounds_json
    corners = [
        align_point((old_bounds[x_key], old_bounds[y_key]))
        for x_key in ("min_x", "max_x")
        for y_key in ("min_y", "max_y")
    ]
    layout.bounds_json = {
        "min_x": min(point[0] for point in corners),
        "min_y": min(point[1] for point in corners),
        "max_x": max(point[0] for point in corners),
        "max_y": max(point[1] for point in corners),
    }
    if not layout.name.endswith("（按三楼实测坐标校准）"):
        layout.name += "（按三楼实测坐标校准）"
    layout.warnings_json = [
        *[warning for warning in layout.warnings_json if not warning.startswith("COORDINATE_FRAME:")],
        COORDINATE_FRAME_MARKER,
        "一楼候选坐标已按三楼实测柱网换算；原始DXF未修改，可由校准前备份恢复。",
    ]
    for placement in layout.placements:
        placement.x_mm, placement.y_mm = align_point((placement.x_mm, placement.y_mm))
        placement.rotation_deg = _rotate_clockwise_storage_angle(placement.rotation_deg, coordinate_rotation_deg)
        placement.version += 1
    for rack in layout.racks:
        rack.x_mm, rack.y_mm = align_point((rack.x_mm, rack.y_mm))
        rack.rotation_deg = _rotate_clockwise_storage_angle(rack.rotation_deg, coordinate_rotation_deg)
        rack.access_side = _rotate_access_side(rack.access_side, coordinate_rotation_deg)
        rack.version += 1
    for pallet in layout.pallets:
        pallet.x_mm, pallet.y_mm = align_point((pallet.x_mm, pallet.y_mm))
        pallet.rotation_deg = _rotate_clockwise_storage_angle(pallet.rotation_deg, coordinate_rotation_deg)
        pallet.version += 1
    for feature in layout.features:
        feature.points_json = [list(align_point((float(x), float(y)))) for x, y in feature.points_json]
        if len(feature.points_json) >= 3:
            feature.area_mm2 = _polygon_area(feature.points_json)
        elif len(feature.points_json) == 2 and feature.width_mm:
            feature.area_mm2 = math.dist(feature.points_json[0], feature.points_json[1]) * feature.width_mm
        feature.version += 1


def _json_report(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "align_point"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--one-floor-layout", required=True)
    parser.add_argument("--three-floor-layout", required=True)
    parser.add_argument("--offset-x-mm", type=float, required=True)
    parser.add_argument("--offset-y-mm", type=float, required=True)
    parser.add_argument("--scale-x", type=float, default=1)
    parser.add_argument("--scale-y", type=float, default=1)
    parser.add_argument("--mirror-x", action="store_true")
    parser.add_argument("--mirror-y", action="store_true")
    parser.add_argument("--rotation-deg", type=float, required=True)
    parser.add_argument("--column-tolerance-mm", type=float, default=1000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=Path("factory_twin/data/backups"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    engine = create_database_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    session_factory = create_session_factory(engine)
    calibration = Calibration(
        offset_x_mm=args.offset_x_mm,
        offset_y_mm=args.offset_y_mm,
        scale_x=args.scale_x,
        scale_y=args.scale_y,
        mirror_x=args.mirror_x,
        mirror_y=args.mirror_y,
        rotation_deg=args.rotation_deg,
    )
    with session_factory() as session:
        one = session.scalar(select(Layout).where(Layout.id == args.one_floor_layout))
        three = session.scalar(select(Layout).where(Layout.id == args.three_floor_layout))
        if one is None or three is None:
            raise RuntimeError("requested 1F or 3F layout does not exist")
        if one.floor_code.upper() != "1F" or three.floor_code.upper() != "3F":
            raise RuntimeError("layout floor codes do not match 1F -> 3F alignment")
        plan = build_alignment_plan(one, three, calibration, column_tolerance_mm=args.column_tolerance_mm)
        report = _json_report(plan)
        if args.apply:
            if args.confirmation != "APPLY_1F_TO_3F_CANDIDATE":
                raise RuntimeError("explicit candidate alignment confirmation is required")
            if not plan.get("safe_to_apply"):
                raise RuntimeError(json.dumps(report.get("blockers", []), ensure_ascii=False))
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            args.backup_dir.mkdir(parents=True, exist_ok=True)
            database_backup = args.backup_dir / f"1f-before-3f-alignment-{stamp}.sqlite3"
            layout_snapshot = args.backup_dir / f"1f-before-3f-alignment-{stamp}.json"
            backup_verification = _verified_sqlite_backup(database, database_backup)
            layout_snapshot.write_text(
                json.dumps(_layout_snapshot(one), ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            apply_alignment(one, plan)
            session.commit()
            report.update({
                "applied": True,
                "database_backup": str(database_backup.resolve()),
                "layout_snapshot": str(layout_snapshot.resolve()),
                "backup_verification": backup_verification,
                "coordinate_frame_marker": COORDINATE_FRAME_MARKER,
            })
            report_path = args.backup_dir / f"1f-3f-alignment-report-{stamp}.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report["report"] = str(report_path.resolve())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("safe_to_apply") else 2


if __name__ == "__main__":
    raise SystemExit(main())

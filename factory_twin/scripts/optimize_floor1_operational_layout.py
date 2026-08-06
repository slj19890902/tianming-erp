"""Tidy the manually arranged 1F zones and merge aisles into one route.

The command is dry-run by default. ``--apply`` first creates and verifies a
full SQLite backup, then updates only the isolated factory-twin candidate
layout. Equipment, racks, pallets and every structure feature stay untouched.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from math import hypot
from pathlib import Path
import sqlite3
from typing import Any


LAYOUT_ID = "756bcfa7-74d8-48f2-ba64-a4a23c9984ae"
ROUTE_CODE = "AISLE-1F-PED-006"
ROUTE_NAME = "一楼连续次通道（人/液压搬运车共用，1500mm）"
ROUTE_POINTS = [
    [-14900, -8000],
    [-4940, -8000],
    [-4940, -24000],
    [-1500, -24000],
]
ROUTE_WIDTH_MM = 1500


def _rect(min_x: int, min_y: int, max_x: int, max_y: int) -> list[list[int]]:
    return [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]


# These coordinates retain each manually arranged block's role and approximate
# position while aligning boundaries to the 1F column/aisle grid. The small
# gaps around the route and column footprints are intentional.
ZONE_TARGETS: dict[str, list[list[int]]] = {
    "ZONE-1F-RAW-002": _rect(-15700, -6700, -14250, -2000),
    "ZONE-1F-RAW-003": _rect(-12800, -7150, -9850, -50),
    "ZONE-1F-RAW-004": _rect(-9500, -7240, -4300, -6520),
    "ZONE-1F-RAW-005": _rect(-22150, -11150, -16400, -8800),
    "ZONE-1F-RAW-006": _rect(-23150, -12950, -20050, -11650),
    "ZONE-1F-RAW-007": _rect(-15350, -27350, -11350, -25000),
    "ZONE-1F-SEMI-001": _rect(-9300, -25000, -6300, -21150),
    "ZONE-1F-SEMI-002": _rect(-14450, -10700, -9350, -9200),
    "ZONE-1F-SEMI-003": _rect(-6700, -5850, -5400, -3400),
    "ZONE-1F-FIN-001": _rect(-1350, -15000, 1100, -9250),
    "ZONE-1F-FIN-002": _rect(-1350, -6900, 1100, -4800),
    "ZONE-1F-FIN-003": _rect(-11900, 700, -5300, 4050),
    "ZONE-1F-TEMP-001": _rect(-1300, -23000, 1100, -15200),
    # Preserve the former triangle's approximately 2.4 m2 allocation.
    "ZONE-1F-TEMP-002": _rect(-15850, 2200, -14450, 3900),
    # Move the narrow strip to the east side of the continuous route.
    "ZONE-1F-TEMP-003": _rect(-15700, -24900, -14300, -19300),
    "ZONE-1F-PLATE-001": _rect(-16300, -28500, -12650, -27400),
    "ZONE-1F-PLATE-002": _rect(-11200, -28250, -8750, -26250),
    "ZONE-1F-MOLD-001": _rect(-4180, -22850, -2780, -18800),
    "ZONE-1F-MOLD-002": _rect(-4150, -18650, -2780, -12300),
}


def polygon_area_mm2(points: list[list[float]]) -> float:
    return abs(
        sum(
            float(x1) * float(y2) - float(x2) * float(y1)
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
        )
    ) / 2


def polyline_length_mm(points: list[list[float]]) -> float:
    return sum(
        hypot(float(x2) - float(x1), float(y2) - float(y1))
        for (x1, y1), (x2, y2) in zip(points, points[1:])
    )


def _segment_rect(
    start: list[float], end: list[float], width_mm: float
) -> tuple[float, float, float, float]:
    padding = width_mm / 2
    return (
        min(float(start[0]), float(end[0])) - padding,
        min(float(start[1]), float(end[1])) - padding,
        max(float(start[0]), float(end[0])) + padding,
        max(float(start[1]), float(end[1])) + padding,
    )


def _rects_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] <= second[0]
        or first[0] >= second[2]
        or first[3] <= second[1]
        or first[1] >= second[3]
    )


def _polygon_bounds(points: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _placement_rect(placement: sqlite3.Row) -> tuple[float, float, float, float]:
    width = float(placement["width_mm"])
    depth = float(placement["depth_mm"])
    if int(placement["rotation_deg"]) % 180 == 90:
        width, depth = depth, width
    x = float(placement["x_mm"])
    y = float(placement["y_mm"])
    return x - width / 2, y - depth / 2, x + width / 2, y + depth / 2


def _is_orthogonal(points: list[list[float]], *, closed: bool) -> bool:
    pairs = zip(points, points[1:] + points[:1]) if closed else zip(points, points[1:])
    return all(float(a[0]) == float(b[0]) or float(a[1]) == float(b[1]) for a, b in pairs)


def _column_rect(feature: sqlite3.Row) -> tuple[float, float, float, float]:
    points = json.loads(feature["points_json"])
    if len(points) < 2:
        raise RuntimeError(f"柱子 {feature['feature_code']} 缺少有效轮廓")
    # Custom columns are stored as a short centreline plus its real width.
    return _segment_rect(points[0], points[-1], float(feature["width_mm"] or 0))


def _segments_cross_non_adjacent(points: list[list[float]]) -> bool:
    rects = [_segment_rect(start, end, 0) for start, end in zip(points, points[1:])]
    for first_index, first in enumerate(rects):
        for second_index, second in enumerate(rects[first_index + 2 :], start=first_index + 2):
            if second_index == first_index + 1:
                continue
            if _rects_intersect(first, second):
                return True
    return False


def _load_features(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return connection.execute(
        """
        SELECT * FROM twin_layout_features
        WHERE layout_id=?
        ORDER BY feature_kind, feature_code
        """,
        (LAYOUT_ID,),
    ).fetchall()


def _build_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    layout = connection.execute(
        "SELECT id, name, floor_code FROM twin_layouts WHERE id=?", (LAYOUT_ID,)
    ).fetchone()
    if layout is None or layout["floor_code"] != "1F":
        raise RuntimeError("找不到指定的一楼候选布局")

    features = _load_features(connection)
    by_code = {feature["feature_code"]: feature for feature in features}
    missing = sorted(set(ZONE_TARGETS) - set(by_code))
    if missing:
        raise RuntimeError(f"缺少一楼区域：{', '.join(missing)}")

    aisles = [feature for feature in features if feature["feature_kind"] == "aisle"]
    route = by_code.get(ROUTE_CODE)
    if route is None or route["feature_kind"] != "aisle":
        raise RuntimeError(f"保留通道 {ROUTE_CODE} 不存在")

    columns = [
        feature
        for feature in features
        if feature["feature_kind"] == "structure" and feature["subtype"] == "custom_column"
    ]
    column_rects = [(column["feature_code"], _column_rect(column)) for column in columns]
    placements = connection.execute(
        """
        SELECT p.*, t.name AS template_name
        FROM twin_equipment_placements AS p
        JOIN twin_asset_templates AS t ON t.id=p.template_id
        WHERE p.layout_id=?
        ORDER BY t.name, p.id
        """,
        (LAYOUT_ID,),
    ).fetchall()
    equipment_rects = [
        (
            placement["template_name"],
            _placement_rect(placement),
            float(placement["z_mm"]),
            float(placement["z_mm"] + placement["height_mm"]),
        )
        for placement in placements
    ]

    if not _is_orthogonal(ROUTE_POINTS, closed=False):
        raise RuntimeError("目标通道不是横平竖直的中心线")
    if _segments_cross_non_adjacent(ROUTE_POINTS):
        raise RuntimeError("目标通道存在非相邻线段交叉或重叠")
    route_rects = [
        _segment_rect(start, end, ROUTE_WIDTH_MM)
        for start, end in zip(ROUTE_POINTS, ROUTE_POINTS[1:])
    ]
    route_column_hits = sorted(
        code
        for code, column_rect in column_rects
        if any(_rects_intersect(segment, column_rect) for segment in route_rects)
    )
    if route_column_hits:
        raise RuntimeError(f"目标通道侵占柱子：{', '.join(route_column_hits)}")
    route_equipment_hits = sorted(
        name
        for name, equipment_rect, _z_low, _z_high in equipment_rects
        if any(_rects_intersect(segment, equipment_rect) for segment in route_rects)
    )
    if route_equipment_hits:
        raise RuntimeError(f"目标通道侵占设备：{', '.join(route_equipment_hits)}")

    zone_column_hits: dict[str, list[str]] = {}
    zone_equipment_hits: dict[str, list[str]] = {}
    route_zone_hits: list[str] = []
    zone_changes: list[dict[str, Any]] = []
    for code, target in ZONE_TARGETS.items():
        if not _is_orthogonal(target, closed=True):
            raise RuntimeError(f"区域 {code} 目标轮廓不是正交多边形")
        target_bounds = _polygon_bounds(target)
        hits = [
            column_code
            for column_code, column_rect in column_rects
            if _rects_intersect(target_bounds, column_rect)
        ]
        if hits:
            zone_column_hits[code] = hits
        feature = by_code[code]
        equipment_hits = [
            name
            for name, equipment_rect, z_low, z_high in equipment_rects
            if _rects_intersect(target_bounds, equipment_rect)
            and (
                feature["storage_mode"] == "floor"
                or (
                    float(feature["elevation_mm"]) < z_high
                    and float(feature["elevation_mm"] + feature["storage_height_mm"]) > z_low
                )
            )
        ]
        if equipment_hits:
            zone_equipment_hits[code] = equipment_hits
        if any(_rects_intersect(target_bounds, segment) for segment in route_rects):
            route_zone_hits.append(code)

        before = json.loads(feature["points_json"])
        after_area = round(polygon_area_mm2(target), 3)
        if before != target or feature["status"] != "candidate" or feature["source"] != "ai":
            zone_changes.append(
                {
                    "id": feature["id"],
                    "feature_code": code,
                    "version_before": feature["version"],
                    "status_before": feature["status"],
                    "points_before": before,
                    "points_after": target,
                    "area_before_m2": round(float(feature["area_mm2"]) / 1_000_000, 3),
                    "area_after_m2": round(after_area / 1_000_000, 3),
                }
            )

    if zone_column_hits:
        raise RuntimeError(f"目标区域侵占柱子：{json.dumps(zone_column_hits, ensure_ascii=False)}")
    if zone_equipment_hits:
        raise RuntimeError(f"目标区域侵占设备：{json.dumps(zone_equipment_hits, ensure_ascii=False)}")
    if route_zone_hits:
        raise RuntimeError(f"目标区域侵占连续通道：{', '.join(sorted(route_zone_hits))}")
    zone_zone_hits = [
        [first_code, second_code]
        for index, (first_code, first_points) in enumerate(ZONE_TARGETS.items())
        for second_code, second_points in list(ZONE_TARGETS.items())[index + 1 :]
        if _rects_intersect(_polygon_bounds(first_points), _polygon_bounds(second_points))
    ]
    if zone_zone_hits:
        raise RuntimeError(f"目标区域相互重叠：{json.dumps(zone_zone_hits, ensure_ascii=False)}")

    route_after = {
        "points": ROUTE_POINTS,
        "width_mm": ROUTE_WIDTH_MM,
        "name": ROUTE_NAME,
        "subtype": "shared_secondary",
        "direction": "two_way",
        "no_stacking": True,
        "color": "#0f9f89",
        "source": "ai",
        "status": "candidate",
        "area_mm2": round(polyline_length_mm(ROUTE_POINTS) * ROUTE_WIDTH_MM, 3),
    }
    route_before = {
        "points": json.loads(route["points_json"]),
        "width_mm": route["width_mm"],
        "name": route["name"],
        "subtype": route["subtype"],
        "direction": route["direction"],
        "no_stacking": bool(route["no_stacking"]),
        "color": route["color"],
        "source": route["source"],
        "status": route["status"],
        "area_mm2": route["area_mm2"],
    }

    return {
        "layout_id": LAYOUT_ID,
        "layout_name": layout["name"],
        "floor_code": layout["floor_code"],
        "zone_updates": zone_changes,
        "zone_area_before_m2": round(
            sum(float(by_code[code]["area_mm2"]) for code in ZONE_TARGETS) / 1_000_000, 3
        ),
        "zone_area_after_m2": round(
            sum(polygon_area_mm2(points) for points in ZONE_TARGETS.values()) / 1_000_000, 3
        ),
        "route_update_required": route_before != route_after,
        "route_version_before": route["version"],
        "route_before": route_before,
        "route_after": route_after,
        "aisle_deletes": [
            {
                "id": aisle["id"],
                "feature_code": aisle["feature_code"],
                "name": aisle["name"],
                "version": aisle["version"],
                "points": json.loads(aisle["points_json"]),
                "width_mm": aisle["width_mm"],
            }
            for aisle in aisles
            if aisle["feature_code"] != ROUTE_CODE
        ],
        "columns_checked": len(columns),
        "route_column_hits": route_column_hits,
        "route_equipment_hits": route_equipment_hits,
        "zone_column_hits": zone_column_hits,
        "zone_equipment_hits": zone_equipment_hits,
        "route_zone_hits": route_zone_hits,
        "zone_zone_hits": zone_zone_hits,
        "equipment_updates": 0,
        "structure_updates": 0,
        "rack_updates": 0,
        "pallet_updates": 0,
        "inventory_rows_written": 0,
    }


def _backup_database(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(f"file:{target_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("一楼优化前备份 quick_check 未通过")
    finally:
        check.close()


def execute(database: Path, *, apply: bool) -> dict[str, Any]:
    database = database.resolve()
    if database.name != "factory_twin.sqlite3":
        raise RuntimeError("只允许处理隔离数字孪生候选库 factory_twin.sqlite3")
    if not database.is_file():
        raise FileNotFoundError(database)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("候选数字孪生数据库 quick_check 未通过")
        plan = _build_plan(connection)
        result = {"mode": "apply" if apply else "dry-run", "database": str(database), **plan}
        if not apply:
            return result

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = database.parent / "backups" / f"before-1f-operational-tidy-{timestamp}.sqlite3"
        plan_path = database.parent / "backups" / f"1f-operational-tidy-plan-{timestamp}.json"
        _backup_database(database, backup_path)
        plan_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connection:
            for item in plan["zone_updates"]:
                cursor = connection.execute(
                    """
                    UPDATE twin_layout_features
                    SET points_json=?, area_mm2=?, source='ai', status='candidate',
                        version=version+1, updated_at=?
                    WHERE id=? AND version=?
                    """,
                    (
                        json.dumps(item["points_after"], ensure_ascii=False),
                        polygon_area_mm2(item["points_after"]),
                        updated_at,
                        item["id"],
                        item["version_before"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"{item['feature_code']} 版本冲突，事务已回滚")

            if plan["route_update_required"]:
                route = plan["route_after"]
                cursor = connection.execute(
                    """
                    UPDATE twin_layout_features
                    SET name=?, subtype=?, points_json=?, width_mm=?, direction=?,
                        no_stacking=?, color=?, area_mm2=?, source='ai', status='candidate',
                        version=version+1, updated_at=?
                    WHERE layout_id=? AND feature_code=? AND version=?
                    """,
                    (
                        route["name"],
                        route["subtype"],
                        json.dumps(route["points"], ensure_ascii=False),
                        route["width_mm"],
                        route["direction"],
                        int(route["no_stacking"]),
                        route["color"],
                        route["area_mm2"],
                        updated_at,
                        LAYOUT_ID,
                        ROUTE_CODE,
                        plan["route_version_before"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"{ROUTE_CODE} 版本冲突，事务已回滚")

            for item in plan["aisle_deletes"]:
                cursor = connection.execute(
                    "DELETE FROM twin_layout_features WHERE id=? AND version=?",
                    (item["id"], item["version"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"{item['feature_code']} 版本冲突，事务已回滚")

            connection.execute(
                "UPDATE twin_layouts SET updated_at=? WHERE id=?", (updated_at, LAYOUT_ID)
            )

        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("一楼优化后 quick_check 未通过")
        after = _build_plan(connection)
        if after["zone_updates"] or after["route_update_required"] or after["aisle_deletes"]:
            raise RuntimeError("一楼优化结果未达到幂等状态")

        result["backup"] = str(backup_path.resolve())
        result["plan"] = str(plan_path.resolve())
        result["post_check"] = {
            "zone_updates_remaining": 0,
            "aisle_deletes_remaining": 0,
            "aisle_count": 1,
            "columns_checked": after["columns_checked"],
            "zone_column_hits": after["zone_column_hits"],
            "route_column_hits": after["route_column_hits"],
            "route_equipment_hits": after["route_equipment_hits"],
            "route_zone_hits": after["route_zone_hits"],
            "zone_equipment_hits": after["zone_equipment_hits"],
            "zone_zone_hits": after["zone_zone_hits"],
        }
        return result
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="整理一楼区域并合并为一条连续正交通道")
    value.add_argument("--database", type=Path, required=True)
    value.add_argument("--apply", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(execute(args.database, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

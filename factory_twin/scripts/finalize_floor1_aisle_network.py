"""Straighten the owner-drawn 1F aisle network and confirm the 1F layout.

Dry-run is the default. ``--apply`` creates and verifies a full SQLite backup
before changing only 1F aisle geometry and zone/aisle confirmation states in
the isolated factory-twin candidate database.
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

AISLE_TARGETS: dict[str, list[list[int]]] = {
    "AISLE-1F-PED-006": [
        [-15300, -8000],
        [-4940, -8000],
        [-4940, -24000],
        [-1500, -24000],
    ],
    "AISLE-1F-MAIN-001": [[-13300, 250], [-13300, -8000]],
    "AISLE-1F-MAIN-002": [[-3350, -4700], [400, -4700]],
    "AISLE-1F-SECONDARY-001": [[-15300, -8000], [-15300, -25700]],
    "AISLE-1F-SECONDARY-002": [[-15300, -11100], [-21900, -11100]],
    "AISLE-1F-SECONDARY-003": [[-15300, -25700], [-11150, -25700]],
    # The short dogleg keeps the centreline connected while clearing the
    # nearby nailing machine footprint.
    "AISLE-1F-SECONDARY-004": [[-4940, -8000], [-4940, -7300], [-3350, -7300]],
    "AISLE-1F-SECONDARY-005": [[-3350, -7300], [-3350, -3100]],
}


def polyline_length_mm(points: list[list[float]]) -> float:
    return sum(
        hypot(float(x2) - float(x1), float(y2) - float(y1))
        for (x1, y1), (x2, y2) in zip(points, points[1:])
    )


def _is_orthogonal(points: list[list[float]]) -> bool:
    return all(
        float(start[0]) == float(end[0]) or float(start[1]) == float(end[1])
        for start, end in zip(points, points[1:])
    )


def _axis_footprint(
    start: list[float], end: list[float], width_mm: float
) -> tuple[float, float, float, float]:
    half = width_mm / 2
    if float(start[1]) == float(end[1]):
        return (
            min(float(start[0]), float(end[0])),
            float(start[1]) - half,
            max(float(start[0]), float(end[0])),
            float(start[1]) + half,
        )
    if float(start[0]) == float(end[0]):
        return (
            float(start[0]) - half,
            min(float(start[1]), float(end[1])),
            float(start[0]) + half,
            max(float(start[1]), float(end[1])),
        )
    raise RuntimeError("只允许横平竖直的目标线段")


def _bounds(points: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


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


def _segments_intersect(
    first: tuple[list[float], list[float]],
    second: tuple[list[float], list[float]],
) -> bool:
    a, b = first
    c, d = second
    first_horizontal = float(a[1]) == float(b[1])
    second_horizontal = float(c[1]) == float(d[1])
    if first_horizontal and second_horizontal:
        return float(a[1]) == float(c[1]) and max(
            min(float(a[0]), float(b[0])), min(float(c[0]), float(d[0]))
        ) <= min(max(float(a[0]), float(b[0])), max(float(c[0]), float(d[0])))
    if not first_horizontal and not second_horizontal:
        return float(a[0]) == float(c[0]) and max(
            min(float(a[1]), float(b[1])), min(float(c[1]), float(d[1]))
        ) <= min(max(float(a[1]), float(b[1])), max(float(c[1]), float(d[1])))
    horizontal = first if first_horizontal else second
    vertical = second if first_horizontal else first
    return (
        min(float(horizontal[0][0]), float(horizontal[1][0]))
        <= float(vertical[0][0])
        <= max(float(horizontal[0][0]), float(horizontal[1][0]))
        and min(float(vertical[0][1]), float(vertical[1][1]))
        <= float(horizontal[0][1])
        <= max(float(vertical[0][1]), float(vertical[1][1]))
    )


def _positive_colinear_overlap(
    first: tuple[list[float], list[float]],
    second: tuple[list[float], list[float]],
) -> bool:
    a, b = first
    c, d = second
    if float(a[1]) == float(b[1]) == float(c[1]) == float(d[1]):
        overlap = min(max(float(a[0]), float(b[0])), max(float(c[0]), float(d[0]))) - max(
            min(float(a[0]), float(b[0])), min(float(c[0]), float(d[0]))
        )
        return overlap > 0
    if float(a[0]) == float(b[0]) == float(c[0]) == float(d[0]):
        overlap = min(max(float(a[1]), float(b[1])), max(float(c[1]), float(d[1]))) - max(
            min(float(a[1]), float(b[1])), min(float(c[1]), float(d[1]))
        )
        return overlap > 0
    return False


def _segments(points: list[list[float]]) -> list[tuple[list[float], list[float]]]:
    return list(zip(points, points[1:]))


def _network_connected() -> bool:
    codes = list(AISLE_TARGETS)
    neighbours = {code: set() for code in codes}
    for index, first_code in enumerate(codes):
        for second_code in codes[index + 1 :]:
            if any(
                _segments_intersect(first, second)
                for first in _segments(AISLE_TARGETS[first_code])
                for second in _segments(AISLE_TARGETS[second_code])
            ):
                neighbours[first_code].add(second_code)
                neighbours[second_code].add(first_code)
    visited = set()
    stack = [codes[0]]
    while stack:
        code = stack.pop()
        if code in visited:
            continue
        visited.add(code)
        stack.extend(neighbours[code] - visited)
    return len(visited) == len(codes)


def _placement_rect(placement: sqlite3.Row) -> tuple[float, float, float, float]:
    width = float(placement["width_mm"])
    depth = float(placement["depth_mm"])
    if int(placement["rotation_deg"]) % 180 == 90:
        width, depth = depth, width
    x = float(placement["x_mm"])
    y = float(placement["y_mm"])
    return x - width / 2, y - depth / 2, x + width / 2, y + depth / 2


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
            raise RuntimeError("一楼通道确认前备份 quick_check 未通过")
    finally:
        check.close()


def _build_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    layout = connection.execute(
        "SELECT id, name, floor_code FROM twin_layouts WHERE id=?", (LAYOUT_ID,)
    ).fetchone()
    if layout is None or layout["floor_code"] != "1F":
        raise RuntimeError("找不到指定的一楼候选布局")

    features = connection.execute(
        "SELECT * FROM twin_layout_features WHERE layout_id=? ORDER BY feature_code",
        (LAYOUT_ID,),
    ).fetchall()
    by_code = {feature["feature_code"]: feature for feature in features}
    aisles = [feature for feature in features if feature["feature_kind"] == "aisle"]
    zones = [feature for feature in features if feature["feature_kind"] == "zone"]
    if set(feature["feature_code"] for feature in aisles) != set(AISLE_TARGETS):
        raise RuntimeError("一楼通道集合已变化，请重新读取现场布局后再确认")
    if len(zones) != 19:
        raise RuntimeError(f"预期一楼 19 个区域，当前为 {len(zones)} 个")
    if not all(_is_orthogonal(points) for points in AISLE_TARGETS.values()):
        raise RuntimeError("目标通道存在斜线")
    if not _network_connected():
        raise RuntimeError("目标通道没有组成一个连通网络")

    all_segments = [
        (code, index, segment)
        for code, points in AISLE_TARGETS.items()
        for index, segment in enumerate(_segments(points))
    ]
    overlaps = []
    for index, (first_code, first_index, first) in enumerate(all_segments):
        for second_code, second_index, second in all_segments[index + 1 :]:
            if first_code == second_code and abs(first_index - second_index) <= 1:
                continue
            if _positive_colinear_overlap(first, second):
                overlaps.append([first_code, second_code])
    if overlaps:
        raise RuntimeError(f"目标通道存在重复覆盖线段：{json.dumps(overlaps, ensure_ascii=False)}")

    columns = [
        feature
        for feature in features
        if feature["feature_kind"] == "structure" and feature["subtype"] == "custom_column"
    ]
    column_rects = [
        (
            feature["feature_code"],
            _axis_footprint(
                json.loads(feature["points_json"])[0],
                json.loads(feature["points_json"])[-1],
                float(feature["width_mm"] or 0),
            ),
        )
        for feature in columns
    ]
    placements = connection.execute(
        """
        SELECT p.*, t.name AS template_name
        FROM twin_equipment_placements AS p
        JOIN twin_asset_templates AS t ON t.id=p.template_id
        WHERE p.layout_id=?
        """,
        (LAYOUT_ID,),
    ).fetchall()
    equipment_rects = [
        (placement["template_name"], _placement_rect(placement)) for placement in placements
    ]

    aisle_column_hits: dict[str, list[str]] = {}
    aisle_equipment_hits: dict[str, list[str]] = {}
    aisle_zone_hits: dict[str, list[str]] = {}
    for code, points in AISLE_TARGETS.items():
        feature = by_code[code]
        lane_rects = [
            _axis_footprint(start, end, float(feature["width_mm"] or 0))
            for start, end in _segments(points)
        ]
        column_hits = [
            column_code
            for column_code, column_rect in column_rects
            if any(_rects_intersect(lane_rect, column_rect) for lane_rect in lane_rects)
        ]
        equipment_hits = [
            equipment_name
            for equipment_name, equipment_rect in equipment_rects
            if any(_rects_intersect(lane_rect, equipment_rect) for lane_rect in lane_rects)
        ]
        zone_hits = [
            zone["feature_code"]
            for zone in zones
            if any(
                _rects_intersect(lane_rect, _bounds(json.loads(zone["points_json"])))
                for lane_rect in lane_rects
            )
        ]
        if column_hits:
            aisle_column_hits[code] = column_hits
        if equipment_hits:
            aisle_equipment_hits[code] = equipment_hits
        if zone_hits:
            aisle_zone_hits[code] = zone_hits
    if aisle_column_hits:
        raise RuntimeError(f"目标通道侵占柱子：{json.dumps(aisle_column_hits, ensure_ascii=False)}")
    if aisle_equipment_hits:
        raise RuntimeError(f"目标通道侵占设备：{json.dumps(aisle_equipment_hits, ensure_ascii=False)}")

    aisle_updates = []
    for code, target in AISLE_TARGETS.items():
        feature = by_code[code]
        before = json.loads(feature["points_json"])
        area_after = round(polyline_length_mm(target) * float(feature["width_mm"] or 0), 3)
        if before != target or feature["status"] != "confirmed" or float(feature["area_mm2"]) != area_after:
            aisle_updates.append(
                {
                    "id": feature["id"],
                    "feature_code": code,
                    "version_before": feature["version"],
                    "status_before": feature["status"],
                    "points_before": before,
                    "points_after": target,
                    "area_after": area_after,
                }
            )

    zone_confirmations = [
        {
            "id": zone["id"],
            "feature_code": zone["feature_code"],
            "version_before": zone["version"],
            "status_before": zone["status"],
        }
        for zone in zones
        if zone["status"] != "confirmed"
    ]
    return {
        "layout_id": LAYOUT_ID,
        "layout_name": layout["name"],
        "floor_code": layout["floor_code"],
        "aisle_updates": aisle_updates,
        "zone_confirmations": zone_confirmations,
        "aisle_count": len(aisles),
        "zone_count": len(zones),
        "network_connected": True,
        "duplicate_segment_overlaps": overlaps,
        "columns_checked": len(columns),
        "equipment_checked": len(placements),
        "aisle_column_hits": aisle_column_hits,
        "aisle_equipment_hits": aisle_equipment_hits,
        # Some owner-drawn internal roads intentionally run through storage
        # polygons. Keep this as advisory evidence rather than moving zones.
        "aisle_zone_hits_advisory": aisle_zone_hits,
        "equipment_updates": 0,
        "structure_updates": 0,
        "inventory_rows_written": 0,
    }


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
        backup_path = database.parent / "backups" / f"before-1f-aisle-confirm-{timestamp}.sqlite3"
        plan_path = database.parent / "backups" / f"1f-aisle-confirm-plan-{timestamp}.json"
        _backup_database(database, backup_path)
        plan_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with connection:
            for item in plan["aisle_updates"]:
                cursor = connection.execute(
                    """
                    UPDATE twin_layout_features
                    SET points_json=?, area_mm2=?, status='confirmed',
                        version=version+1, updated_at=?
                    WHERE id=? AND version=?
                    """,
                    (
                        json.dumps(item["points_after"], ensure_ascii=False),
                        item["area_after"],
                        updated_at,
                        item["id"],
                        item["version_before"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"{item['feature_code']} 版本冲突，事务已回滚")
            for item in plan["zone_confirmations"]:
                cursor = connection.execute(
                    """
                    UPDATE twin_layout_features
                    SET status='confirmed', version=version+1, updated_at=?
                    WHERE id=? AND version=?
                    """,
                    (updated_at, item["id"], item["version_before"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"{item['feature_code']} 版本冲突，事务已回滚")
            connection.execute(
                "UPDATE twin_layouts SET updated_at=? WHERE id=?", (updated_at, LAYOUT_ID)
            )

        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("一楼布局确认后 quick_check 未通过")
        after = _build_plan(connection)
        if after["aisle_updates"] or after["zone_confirmations"]:
            raise RuntimeError("一楼布局确认结果未达到幂等状态")
        result["backup"] = str(backup_path.resolve())
        result["plan"] = str(plan_path.resolve())
        result["post_check"] = {
            "aisle_updates_remaining": 0,
            "zone_confirmations_remaining": 0,
            "aisle_count": after["aisle_count"],
            "zone_count": after["zone_count"],
            "network_connected": after["network_connected"],
            "duplicate_segment_overlaps": after["duplicate_segment_overlaps"],
            "aisle_column_hits": after["aisle_column_hits"],
            "aisle_equipment_hits": after["aisle_equipment_hits"],
            "aisle_zone_hits_advisory": after["aisle_zone_hits_advisory"],
        }
        return result
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="校直一楼道路网络并确认一楼区域和通道")
    value.add_argument("--database", type=Path, required=True)
    value.add_argument("--apply", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(execute(args.database, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

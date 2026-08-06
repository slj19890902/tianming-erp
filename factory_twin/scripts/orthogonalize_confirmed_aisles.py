from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from math import hypot
from pathlib import Path
import sqlite3


def orthogonalize_polyline(points: list[list[float]]) -> list[list[int]]:
    """Snap a near-orthogonal centreline without changing its route topology."""
    if len(points) < 2:
        return [[round(value) for value in point] for point in points]
    axes: list[str] = []
    values: list[float] = []
    for start, end in zip(points, points[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        axis = "horizontal" if abs(dx) >= abs(dy) else "vertical"
        axes.append(axis)
        values.append((start[1] + end[1]) / 2 if axis == "horizontal" else (start[0] + end[0]) / 2)

    result: list[list[int]] = []
    for index, point in enumerate(points):
        x, y = point
        adjacent = []
        if index > 0:
            adjacent.append((axes[index - 1], values[index - 1]))
        if index < len(axes):
            adjacent.append((axes[index], values[index]))
        vertical = [value for axis, value in adjacent if axis == "vertical"]
        horizontal = [value for axis, value in adjacent if axis == "horizontal"]
        if vertical:
            x = sum(vertical) / len(vertical)
        if horizontal:
            y = sum(horizontal) / len(horizontal)
        result.append([round(x), round(y)])
    return result


def _plan(connection: sqlite3.Connection, maximum_shift_mm: float) -> list[dict]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT f.id, f.feature_code, f.points_json, f.width_mm, f.version, l.floor_code
        FROM twin_layout_features AS f
        JOIN twin_layouts AS l ON l.id = f.layout_id
        WHERE f.feature_kind = 'aisle' AND f.feature_code LIKE 'AISLE-%'
        ORDER BY l.floor_code, f.feature_code
        """
    ).fetchall()
    changes = []
    for row in rows:
        before = json.loads(row["points_json"])
        after = orthogonalize_polyline(before)
        if after == before:
            continue
        shifts = [hypot(new[0] - old[0], new[1] - old[1]) for old, new in zip(before, after)]
        maximum = max(shifts, default=0)
        if maximum > maximum_shift_mm:
            raise RuntimeError(
                f"{row['feature_code']} 校直位移 {maximum:.1f} mm 超过上限 {maximum_shift_mm:.1f} mm"
            )
        changes.append(
            {
                "id": row["id"],
                "floor_code": row["floor_code"],
                "feature_code": row["feature_code"],
                "width_mm": row["width_mm"],
                "version_before": row["version"],
                "maximum_point_shift_mm": round(maximum, 1),
                "points_before": before,
                "points_after": after,
            }
        )
    return changes


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
            raise RuntimeError("通道校直前备份 quick_check 未通过")
    finally:
        check.close()


def execute(database: Path, *, apply: bool, maximum_shift_mm: float = 400) -> dict:
    if not database.is_file():
        raise FileNotFoundError(database)
    connection = sqlite3.connect(database)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("候选数字孪生数据库 quick_check 未通过")
        changes = _plan(connection, maximum_shift_mm)
        result = {
            "mode": "apply" if apply else "dry-run",
            "database": str(database),
            "changed_aisles": len(changes),
            "floors": {
                floor: sum(item["floor_code"] == floor for item in changes)
                for floor in ("1F", "3F")
            },
            "maximum_point_shift_mm": max(
                (item["maximum_point_shift_mm"] for item in changes), default=0
            ),
            "widths_preserved": True,
            "changes": changes,
            "inventory_rows_written": 0,
        }
        if not apply:
            return result

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = database.parent / "backups" / f"before-aisle-orthogonalization-{timestamp}.sqlite3"
        plan_path = database.parent / "backups" / f"aisle-orthogonalization-plan-{timestamp}.json"
        _backup_database(database, backup_path)
        plan_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connection:
            for item in changes:
                cursor = connection.execute(
                    """
                    UPDATE twin_layout_features
                    SET points_json=?, version=version+1, updated_at=?
                    WHERE id=? AND version=?
                    """,
                    (
                        json.dumps(item["points_after"], ensure_ascii=False),
                        updated_at,
                        item["id"],
                        item["version_before"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"{item['feature_code']} 版本冲突，已回滚")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("通道校直后 quick_check 未通过")
        result["backup"] = str(backup_path)
        result["plan"] = str(plan_path)
        return result
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="将既有通道中心线校直为横平竖直")
    value.add_argument("--database", type=Path, required=True)
    value.add_argument("--apply", action="store_true")
    value.add_argument("--maximum-shift-mm", type=float, default=400)
    return value


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(execute(args.database, apply=args.apply, maximum_shift_mm=args.maximum_shift_mm), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

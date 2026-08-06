"""Unify the confirmed shared 1F/3F freight elevator on the 3F geometry.

The measured 3F elevator is authoritative. ``--apply`` changes only the 1F
candidate elevator after writing a verified SQLite backup and full JSON
snapshots of both floors. It never moves either floor, its column grid, walls,
equipment, zones, aisles, racks or pallets, and it never touches ERP data.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .optimize_floor3_layout import _request
except ImportError:
    from optimize_floor3_layout import _request


DEFAULT_ONE_FLOOR_LAYOUT = "756bcfa7-74d8-48f2-ba64-a4a23c9984ae"
DEFAULT_THREE_FLOOR_LAYOUT = "edfd9fd4-f013-41cf-b491-cd9d5dce6f52"
CONFIRMATION = "ALIGN_SHARED_LIFT_TO_3F_CANDIDATE"
GEOMETRY_FIELDS = (
    "feature_code", "name", "points", "width_mm", "elevation_mm",
    "storage_height_mm", "color",
)


def _lift(layout: dict[str, Any]) -> dict[str, Any]:
    lifts = [
        feature for feature in layout.get("features", [])
        if feature.get("feature_kind") == "structure"
        and feature.get("subtype") == "freight_elevator"
        and str(feature.get("feature_code", "")).upper().startswith("LIFT-")
    ]
    if len(lifts) != 1:
        raise RuntimeError(
            f"{layout.get('floor_code')} requires exactly one structure freight elevator; found {len(lifts)}"
        )
    return lifts[0]


def _center(feature: dict[str, Any]) -> list[float]:
    xs = [float(point[0]) for point in feature["points"]]
    ys = [float(point[1]) for point in feature["points"]]
    return [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2]


def _footprint(feature: dict[str, Any]) -> dict[str, float]:
    start, end = feature["points"][0], feature["points"][-1]
    return {
        "segment_length_mm": math.dist(start, end),
        "depth_mm": float(feature["width_mm"]),
        "height_mm": float(feature["storage_height_mm"]),
        "angle_deg": math.degrees(
            math.atan2(float(end[1]) - float(start[1]), float(end[0]) - float(start[0]))
        ),
    }


def build_plan(one: dict[str, Any], three: dict[str, Any]) -> dict[str, Any]:
    if one.get("floor_code") != "1F" or three.get("floor_code") != "3F":
        raise RuntimeError("layout floor codes must be 1F and 3F")
    follower = _lift(one)
    authority = _lift(three)
    duplicate = next(
        (
            feature for feature in one.get("features", [])
            if feature["id"] != follower["id"]
            and feature.get("feature_code") == authority["feature_code"]
        ),
        None,
    )
    if duplicate is not None:
        raise RuntimeError(
            f"1F already contains another {authority['feature_code']} feature: {duplicate['id']}"
        )
    if follower.get("status") == "confirmed" and follower["feature_code"] != authority["feature_code"]:
        raise RuntimeError("confirmed 1F elevator cannot be silently renumbered")

    desired = {field: authority[field] for field in GEOMETRY_FIELDS}
    changes = {
        field: value for field, value in desired.items()
        if follower.get(field) != value
    }
    before_center = _center(follower)
    authority_center = _center(authority)
    return {
        "one_floor_layout_id": one["id"],
        "three_floor_layout_id": three["id"],
        "authority_floor": "3F",
        "authority_feature_id": authority["id"],
        "authority_code": authority["feature_code"],
        "follower_feature": follower,
        "payload": changes,
        "summary": {
            "needs_update": bool(changes),
            "old_code": follower["feature_code"],
            "new_code": authority["feature_code"],
            "center_distance_before_mm": round(math.dist(before_center, authority_center), 3),
            "center_distance_after_mm": 0,
            "one_floor_footprint_before": _footprint(follower),
            "shared_footprint_after": _footprint(authority),
            "one_floor_other_feature_updates": 0,
            "three_floor_updates": 0,
            "equipment_moves": 0,
            "column_moves": 0,
            "erp_writes": 0,
        },
    }


def _verified_sqlite_backup(database: Path, backup: Path) -> dict[str, Any]:
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
        quick_check = target.execute("PRAGMA quick_check").fetchone()[0]
        source_tables = source.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        backup_tables = target.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        if quick_check != "ok" or source_tables != backup_tables:
            raise RuntimeError("SQLite backup verification failed")
        return {
            "quick_check": quick_check,
            "table_count": backup_tables,
            "size_bytes": backup.stat().st_size,
        }
    finally:
        source.close()
        target.close()


def _source_quick_check(database: Path) -> str:
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()


def _restore_payload(before: dict[str, Any], version: int) -> dict[str, Any]:
    return {"version": version, **{field: before[field] for field in GEOMETRY_FIELDS}}


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "one_floor_layout_id": plan["one_floor_layout_id"],
        "three_floor_layout_id": plan["three_floor_layout_id"],
        "authority_floor": plan["authority_floor"],
        "authority_code": plan["authority_code"],
        "changed_fields": sorted(plan["payload"]),
        "summary": plan["summary"],
    }


def _geometry_matches(one: dict[str, Any], three: dict[str, Any]) -> bool:
    one_lift, three_lift = _lift(one), _lift(three)
    return all(one_lift.get(field) == three_lift.get(field) for field in GEOMETRY_FIELDS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--one-floor-layout", default=DEFAULT_ONE_FLOOR_LAYOUT)
    parser.add_argument("--three-floor-layout", default=DEFAULT_THREE_FLOOR_LAYOUT)
    parser.add_argument("--api-base", default="http://127.0.0.1:8092/api")
    parser.add_argument("--editor-token", default="local-mvp-token")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, default=Path("factory_twin/data/backups"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    one = _request(f"{args.api_base}/layouts/{args.one_floor_layout}")
    three = _request(f"{args.api_base}/layouts/{args.three_floor_layout}")
    plan = build_plan(one, three)
    report = {**_public_plan(plan), "applied": False}
    if args.apply and plan["summary"]["needs_update"]:
        if args.confirmation != CONFIRMATION:
            raise RuntimeError(f"explicit confirmation {CONFIRMATION!r} is required")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        database_backup = args.backup_dir / f"before-shared-lift-alignment-{stamp}.sqlite3"
        one_snapshot = args.backup_dir / f"1f-before-shared-lift-alignment-{stamp}.json"
        three_snapshot = args.backup_dir / f"3f-authority-shared-lift-{stamp}.json"
        backup_verification = _verified_sqlite_backup(database, database_backup)
        one_snapshot.write_text(json.dumps(one, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        three_snapshot.write_text(json.dumps(three, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        updated: dict[str, Any] | None = None
        try:
            follower = plan["follower_feature"]
            updated = _request(
                f"{args.api_base}/features/{follower['id']}",
                "PATCH",
                args.editor_token,
                {"version": follower["version"], **plan["payload"]},
            )
            one_after = _request(f"{args.api_base}/layouts/{args.one_floor_layout}")
            three_after = _request(f"{args.api_base}/layouts/{args.three_floor_layout}")
            if three_after != three:
                raise RuntimeError("authoritative 3F layout changed during shared-lift alignment")
            if not _geometry_matches(one_after, three_after):
                raise RuntimeError("1F and 3F freight-elevator geometry did not converge exactly")
        except Exception:
            if updated is not None:
                _request(
                    f"{args.api_base}/features/{updated['id']}",
                    "PATCH",
                    args.editor_token,
                    _restore_payload(plan["follower_feature"], updated["version"]),
                )
            raise
        report.update({
            "applied": True,
            "updated_feature_id": updated["id"],
            "updated_version": updated["version"],
            "database_backup": str(database_backup.resolve()),
            "one_floor_snapshot": str(one_snapshot.resolve()),
            "three_floor_snapshot": str(three_snapshot.resolve()),
            "backup_verification": backup_verification,
            "source_quick_check": _source_quick_check(database),
            "exact_geometry_match": True,
        })
        report_path = args.backup_dir / f"shared-lift-alignment-report-{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["report"] = str(report_path.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

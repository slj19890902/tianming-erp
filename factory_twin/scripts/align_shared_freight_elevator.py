"""Align the shared 1F/4F freight elevators to authoritative 3F geometry.

The measured 3F elevator is authoritative. ``--apply`` changes only explicitly
selected 1F and/or 4F follower elevators after writing a verified SQLite backup
and full JSON snapshots of every involved floor. It never moves a floor, its
column grid, walls, equipment, zones, aisles, racks or pallets, and it never
touches ERP data.
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
FOLLOWER_FLOOR_CODES = frozenset({"1F", "4F"})
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


def build_plan(follower_layout: dict[str, Any], authority_layout: dict[str, Any]) -> dict[str, Any]:
    follower_floor = str(follower_layout.get("floor_code") or "").upper()
    authority_floor = str(authority_layout.get("floor_code") or "").upper()
    if follower_floor not in FOLLOWER_FLOOR_CODES or authority_floor != "3F":
        raise RuntimeError("authority floor must be 3F and follower floor must be 1F or 4F")
    if follower_layout.get("id") == authority_layout.get("id"):
        raise RuntimeError("authority and follower layouts must be different")
    follower = _lift(follower_layout)
    authority = _lift(authority_layout)
    duplicate = next(
        (
            feature for feature in follower_layout.get("features", [])
            if feature["id"] != follower["id"]
            and str(feature.get("feature_code") or "").upper()
            == str(authority["feature_code"]).upper()
        ),
        None,
    )
    if duplicate is not None:
        raise RuntimeError(
            f"{follower_floor} already contains another {authority['feature_code']} feature: {duplicate['id']}"
        )
    if (
        follower.get("status") == "confirmed"
        and str(follower["feature_code"]).upper()
        != str(authority["feature_code"]).upper()
    ):
        raise RuntimeError(
            f"confirmed {follower_floor} elevator cannot be silently renumbered"
        )

    desired = {field: authority[field] for field in GEOMETRY_FIELDS}
    changes = {
        field: value for field, value in desired.items()
        if follower.get(field) != value
    }
    before_center = _center(follower)
    authority_center = _center(authority)
    plan = {
        "follower_floor_layout_id": follower_layout["id"],
        "authority_layout_id": authority_layout["id"],
        "three_floor_layout_id": authority_layout["id"],
        "follower_floor": follower_floor,
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
            "follower_footprint_before": _footprint(follower),
            "shared_footprint_after": _footprint(authority),
            "follower_other_feature_updates": 0,
            "three_floor_updates": 0,
            "equipment_moves": 0,
            "column_moves": 0,
            "erp_writes": 0,
        },
    }
    if follower_floor == "1F":
        # Preserve the established report fields consumed by the original 1F
        # runbook while exposing floor-neutral fields for the new 4F follower.
        plan["one_floor_layout_id"] = follower_layout["id"]
        plan["summary"]["one_floor_footprint_before"] = plan["summary"][
            "follower_footprint_before"
        ]
        plan["summary"]["one_floor_other_feature_updates"] = 0
    return plan


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
    result = {
        "follower_floor_layout_id": plan["follower_floor_layout_id"],
        "three_floor_layout_id": plan["three_floor_layout_id"],
        "follower_floor": plan["follower_floor"],
        "authority_floor": plan["authority_floor"],
        "authority_code": plan["authority_code"],
        "changed_fields": sorted(plan["payload"]),
        "summary": plan["summary"],
    }
    if "one_floor_layout_id" in plan:
        result["one_floor_layout_id"] = plan["one_floor_layout_id"]
    return result


def _geometry_matches(follower: dict[str, Any], authority: dict[str, Any]) -> bool:
    follower_lift, authority_lift = _lift(follower), _lift(authority)
    return all(
        follower_lift.get(field) == authority_lift.get(field)
        for field in GEOMETRY_FIELDS
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--one-floor-layout", default=DEFAULT_ONE_FLOOR_LAYOUT)
    parser.add_argument(
        "--four-floor-layout",
        default="",
        help="optional 4F follower layout id; omitted keeps the established 1F-only run",
    )
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
    authority = _request(f"{args.api_base}/layouts/{args.three_floor_layout}")
    follower_layout_ids = [args.one_floor_layout]
    if args.four_floor_layout:
        follower_layout_ids.append(args.four_floor_layout)
    if len(follower_layout_ids) != len(set(follower_layout_ids)):
        raise RuntimeError("follower layout ids must be unique")
    followers = [
        _request(f"{args.api_base}/layouts/{layout_id}")
        for layout_id in follower_layout_ids
    ]
    plans = [build_plan(follower, authority) for follower in followers]
    report: dict[str, Any] = {
        **_public_plan(plans[0]),
        "followers": [_public_plan(plan) for plan in plans],
        "applied": False,
    }
    changed_plans = [plan for plan in plans if plan["summary"]["needs_update"]]
    if args.apply and changed_plans:
        if args.confirmation != CONFIRMATION:
            raise RuntimeError(f"explicit confirmation {CONFIRMATION!r} is required")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        database_backup = args.backup_dir / f"before-shared-lift-alignment-{stamp}.sqlite3"
        three_snapshot = args.backup_dir / f"3f-authority-shared-lift-{stamp}.json"
        backup_verification = _verified_sqlite_backup(database, database_backup)
        three_snapshot.write_text(
            json.dumps(authority, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        follower_snapshots: dict[str, str] = {}
        for follower in followers:
            floor_code = str(follower["floor_code"]).lower()
            snapshot = args.backup_dir / (
                f"{floor_code}-before-shared-lift-alignment-{stamp}.json"
            )
            snapshot.write_text(
                json.dumps(follower, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            follower_snapshots[str(follower["floor_code"])] = str(snapshot.resolve())
        updated_followers: list[tuple[dict[str, Any], dict[str, Any]]] = []
        try:
            for plan in changed_plans:
                follower_feature = plan["follower_feature"]
                updated = _request(
                    f"{args.api_base}/features/{follower_feature['id']}",
                    "PATCH",
                    args.editor_token,
                    {"version": follower_feature["version"], **plan["payload"]},
                )
                updated_followers.append((plan, updated))
            authority_after = _request(
                f"{args.api_base}/layouts/{args.three_floor_layout}"
            )
            if authority_after != authority:
                raise RuntimeError("authoritative 3F layout changed during shared-lift alignment")
            for plan in plans:
                follower_after = _request(
                    f"{args.api_base}/layouts/{plan['follower_floor_layout_id']}"
                )
                if not _geometry_matches(follower_after, authority_after):
                    raise RuntimeError(
                        f"{plan['follower_floor']} and 3F freight-elevator geometry did not converge exactly"
                    )
        except Exception:
            for plan, updated in reversed(updated_followers):
                _request(
                    f"{args.api_base}/features/{updated['id']}",
                    "PATCH",
                    args.editor_token,
                    _restore_payload(plan["follower_feature"], updated["version"]),
                )
            raise
        report.update({
            "applied": True,
            "updated_followers": [
                {
                    "floor_code": plan["follower_floor"],
                    "feature_id": updated["id"],
                    "version": updated["version"],
                }
                for plan, updated in updated_followers
            ],
            "database_backup": str(database_backup.resolve()),
            "follower_snapshots": follower_snapshots,
            "three_floor_snapshot": str(three_snapshot.resolve()),
            "backup_verification": backup_verification,
            "source_quick_check": _source_quick_check(database),
            "exact_geometry_match": True,
        })
        if len(updated_followers) == 1 and updated_followers[0][0]["follower_floor"] == "1F":
            # Preserve the original one-floor report contract for existing
            # factory runbooks that do not request a 4F follower.
            _, updated = updated_followers[0]
            report["updated_feature_id"] = updated["id"]
            report["updated_version"] = updated["version"]
            report["one_floor_snapshot"] = follower_snapshots["1F"]
        report_path = args.backup_dir / f"shared-lift-alignment-report-{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["report"] = str(report_path.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

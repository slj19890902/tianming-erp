"""Apply the owner-confirmed 3F storage-usage correction to the twin candidate.

This tool deliberately leaves equipment, columns, aisles, pallets and every
unrelated zone untouched.  ``--apply`` writes only the candidate twin SQLite
database after a verified SQLite backup and a complete 3F JSON snapshot have
been created.  It never reads or writes ERP inventory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .configure_floor3_modular_racks import _rack
    from .finalize_floor3_operational_plan import LAYOUT_ID
    from .optimize_floor3_layout import _request
except ImportError:
    from configure_floor3_modular_racks import _rack
    from finalize_floor3_operational_plan import LAYOUT_ID
    from optimize_floor3_layout import _request


CONFIRMATION = "APPLY_3F_STORAGE_USAGE_CANDIDATE"
TARGET_FEATURE_CODES = {
    "ZONE-3F-ERP-D1",
    "ZONE-3F-ERP-D2",
    "ZONE-3F-ERP-F1",
    "ZONE-3F-FG-001",
    "ZONE-3F-FIN-003",
    "ZONE-3F-FIN-004",
}
SURPLUS_FEATURE_CODES = {
    "ZONE-3F-FG-001",
    "ZONE-3F-FIN-003",
    "ZONE-3F-FIN-004",
}
NEW_F1_RACK_CODES = {
    "RACK-3F-F1-WEST-SOUTH-001",
    "RACK-3F-F1-WEST-NORTH-001",
}


def _feature_updates(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code = {feature["feature_code"]: feature for feature in features}
    missing = sorted(TARGET_FEATURE_CODES - by_code.keys())
    if missing:
        raise RuntimeError(f"missing 3F feature codes: {missing}")
    desired: dict[str, dict[str, Any]] = {
        "ZONE-3F-ERP-D1": {
            "name": "D1 栈板成品存放区（无货架）",
            "subtype": "finished_storage",
            "storage_mode": "floor",
            "color": "#3b82f6",
        },
        "ZONE-3F-ERP-D2": {
            "name": "D2 货架为主·栈板混合存放区",
            "subtype": "rack_storage",
            # D2 must remain a floor-capable zone so standard pallets can be
            # freely placed beneath and beside its parameterised racks.
            "storage_mode": "floor",
            "color": "#8b5cf6",
        },
        "ZONE-3F-ERP-F1": {
            "name": "F1 两排4货架区（货架可独立移动）",
            "subtype": "rack_storage",
            "storage_mode": "floor",
            "color": "#8b5cf6",
        },
    }
    for code in SURPLUS_FEATURE_CODES:
        desired[code] = {
            "name": "送剩零头纸箱长期存放区（待同订单续配）",
            "subtype": "delivery_surplus",
            "storage_mode": "floor",
            "color": "#f97316",
        }

    f1 = by_code["ZONE-3F-ERP-F1"]
    xs = [float(point[0]) for point in f1["points"]]
    ys = [float(point[1]) for point in f1["points"]]
    max_x = max(xs)
    desired["ZONE-3F-ERP-F1"]["points"] = [
        [max_x - 2200.0, min(ys)],
        [max_x, min(ys)],
        [max_x, max(ys)],
        [max_x - 2200.0, max(ys)],
    ]

    updates: list[dict[str, Any]] = []
    for code, payload in desired.items():
        current = by_code[code]
        changed = {
            key: value for key, value in payload.items()
            if current.get(key) != value
        }
        if changed:
            updates.append({"feature": current, "payload": changed})
    return updates


def _f1_rack_creates(racks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code = {rack["rack_code"]: rack for rack in racks}
    d1_racks = sorted(code for code in by_code if code.startswith("RACK-3F-D1-"))
    if d1_racks:
        raise RuntimeError(f"D1 still contains racks; manual review required: {d1_racks}")
    d2_racks = [code for code in by_code if code.startswith("RACK-3F-D2-")]
    if len(d2_racks) != 5:
        raise RuntimeError(f"D2 expected 5 existing racks, found {len(d2_racks)}")
    east = {
        "SOUTH": by_code.get("RACK-3F-F1-EAST-SOUTH-001"),
        "NORTH": by_code.get("RACK-3F-F1-EAST-NORTH-001"),
    }
    if any(rack is None for rack in east.values()):
        raise RuntimeError("F1 east row must contain north and south modules before adding the west row")

    creates: list[dict[str, Any]] = []
    for segment in ("SOUTH", "NORTH"):
        code = f"RACK-3F-F1-WEST-{segment}-001"
        if code in by_code:
            continue
        source = east[segment]
        creates.append(_rack(
            code,
            f"F1西排{'南段' if segment == 'SOUTH' else '北段'}标准货架",
            -1590,
            float(source["y_mm"]),
            access_side="west",
        ))
    return creates


def build_plan(layout: dict[str, Any]) -> dict[str, Any]:
    if layout.get("floor_code") != "3F":
        raise RuntimeError("only the 3F candidate layout may be refined")
    updates = _feature_updates(layout.get("features", []))
    creates = _f1_rack_creates(layout.get("racks", []))
    return {
        "layout_id": layout["id"],
        "feature_updates": updates,
        "rack_creates": creates,
        "summary": {
            "feature_updates": len(updates),
            "f1_rack_creates": len(creates),
            "equipment_moves": 0,
            "column_moves": 0,
            "aisle_moves": 0,
            "pallet_moves": 0,
            "erp_inventory_writes": 0,
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


def _restore_feature_payload(before: dict[str, Any], version: int) -> dict[str, Any]:
    return {
        "version": version,
        "name": before["name"],
        "subtype": before["subtype"],
        "points": before["points"],
        "width_mm": before["width_mm"],
        "direction": before["direction"],
        "no_stacking": before["no_stacking"],
        "storage_mode": before["storage_mode"],
        "elevation_mm": before["elevation_mm"],
        "storage_height_mm": before["storage_height_mm"],
        "color": before["color"],
    }


def apply_plan(api_base: str, plan: dict[str, Any], token: str) -> dict[str, Any]:
    updated: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    try:
        for item in plan["feature_updates"]:
            feature = item["feature"]
            result = _request(
                f"{api_base}/features/{feature['id']}",
                "PATCH",
                token,
                {"version": feature["version"], **item["payload"]},
            )
            updated.append({"before": feature, "after": result})
        for rack in plan["rack_creates"]:
            result = _request(
                f"{api_base}/layouts/{plan['layout_id']}/racks",
                "POST",
                token,
                rack,
            )
            created.append(result)
        return {
            "updated_feature_codes": [item["after"]["feature_code"] for item in updated],
            "created_rack_codes": [rack["rack_code"] for rack in created],
        }
    except Exception:
        for rack in reversed(created):
            _request(f"{api_base}/racks/{rack['id']}", "DELETE", token)
        for item in reversed(updated):
            _request(
                f"{api_base}/features/{item['before']['id']}",
                "PATCH",
                token,
                _restore_feature_payload(item["before"], item["after"]["version"]),
            )
        raise


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "layout_id": plan["layout_id"],
        "summary": plan["summary"],
        "feature_codes": [item["feature"]["feature_code"] for item in plan["feature_updates"]],
        "rack_codes": [rack["rack_code"] for rack in plan["rack_creates"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-id", default=LAYOUT_ID)
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
    layout = _request(f"{args.api_base}/layouts/{args.layout_id}")
    plan = build_plan(layout)
    report = {**_public_plan(plan), "applied": False}
    if args.apply:
        if args.confirmation != CONFIRMATION:
            raise RuntimeError(f"explicit confirmation {CONFIRMATION!r} is required")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        database_backup = args.backup_dir / f"3f-before-storage-usage-{stamp}.sqlite3"
        layout_snapshot = args.backup_dir / f"3f-before-storage-usage-{stamp}.json"
        backup_verification = _verified_sqlite_backup(database, database_backup)
        layout_snapshot.write_text(
            json.dumps(layout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        result = apply_plan(args.api_base, plan, args.editor_token)
        after = _request(f"{args.api_base}/layouts/{args.layout_id}")
        after_plan = build_plan(after)
        if after_plan["summary"]["feature_updates"] or after_plan["summary"]["f1_rack_creates"]:
            raise RuntimeError("post-apply plan did not converge")
        report.update({
            "applied": True,
            **result,
            "database_backup": str(database_backup.resolve()),
            "layout_snapshot": str(layout_snapshot.resolve()),
            "backup_verification": backup_verification,
            "source_quick_check": _source_quick_check(database),
            "rack_count_after": len(after.get("racks", [])),
        })
        report_path = args.backup_dir / f"3f-storage-usage-report-{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["report"] = str(report_path.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

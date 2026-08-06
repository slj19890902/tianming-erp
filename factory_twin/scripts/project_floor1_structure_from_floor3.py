from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from factory_twin.backend.layout_rules import feature_area_mm2


CONFIRM_PHRASE = "PROJECT 3F STRUCTURE TO 1F"
FRAME_MARKER = "COORDINATE_FRAME:3F_MEASURED_V1"
PROJECTION_MARKER = "STRUCTURE_FRAME:3F_PROJECTED_V1"
LOCKED_STRUCTURE_SUBTYPES = {"custom_column", "freight_elevator"}


def _json(value):
    return json.loads(value) if isinstance(value, str) else deepcopy(value)


def _projected_column(source: dict, floor1_layout_id: str) -> dict:
    code = str(source["feature_code"]).replace("COL-3F-", "COL-1F-", 1)
    return {
        "id": str(uuid4()),
        "layout_id": floor1_layout_id,
        "feature_code": code,
        "name": "三楼柱网投影柱",
        "feature_kind": "structure",
        "subtype": "custom_column",
        "points_json": _json(source["points_json"]),
        "width_mm": float(source["width_mm"]),
        "direction": None,
        "no_stacking": 0,
        "storage_mode": "floor",
        "elevation_mm": 0.0,
        "storage_height_mm": float(source["storage_height_mm"]),
        "color": "#334155",
        "area_mm2": feature_area_mm2(
            "structure", _json(source["points_json"]), float(source["width_mm"])
        ),
        "source": "manual",
        "status": "confirmed",
        "version": 1,
    }


def _projected_dxf_wall(structure: dict, floor1_layout_id: str) -> dict:
    handle = str(structure.get("source_handle") or structure.get("id") or "UNKNOWN")
    geometry = structure.get("geometry") or {}
    points = deepcopy(geometry.get("points") or [])
    thickness = 250.0 if structure.get("kind") == "exterior_wall" else 120.0
    return {
        "id": str(uuid4()),
        "layout_id": floor1_layout_id,
        "feature_code": f"WALL-1F-3F-DXF-{handle}"[:80],
        "name": f"三楼实测投影墙体 {handle}"[:160],
        "feature_kind": "structure",
        "subtype": "custom_wall",
        "points_json": points,
        "width_mm": thickness,
        "direction": None,
        "no_stacking": 0,
        "storage_mode": "floor",
        "elevation_mm": 0.0,
        "storage_height_mm": 3000.0,
        "color": "#64748b",
        "area_mm2": feature_area_mm2("structure", points, thickness),
        "source": "manual",
        "status": "candidate",
        "version": 1,
    }


def _projected_manual_wall(source: dict, floor1_layout_id: str) -> dict:
    suffix = str(source["feature_code"]).removeprefix("STRUCT-3F-")
    points = _json(source["points_json"])
    width = float(source["width_mm"])
    return {
        "id": str(uuid4()),
        "layout_id": floor1_layout_id,
        "feature_code": f"WALL-1F-3F-MANUAL-{suffix}"[:80],
        "name": f"三楼人工墙体投影：{source['name']}"[:160],
        "feature_kind": "structure",
        "subtype": "custom_wall",
        "points_json": points,
        "width_mm": width,
        "direction": None,
        "no_stacking": 0,
        "storage_mode": "floor",
        "elevation_mm": 0.0,
        "storage_height_mm": float(source["storage_height_mm"]),
        "color": "#475569",
        "area_mm2": feature_area_mm2("structure", points, width),
        "source": "manual",
        "status": "candidate",
        "version": 1,
    }


def build_plan(
    *,
    floor1_layout: dict,
    floor3_layout: dict,
    floor1_features: list[dict],
    floor3_features: list[dict],
) -> dict:
    warnings = _json(floor1_layout["warnings_json"])
    if FRAME_MARKER not in warnings:
        raise RuntimeError("一楼尚未进入三楼实测毫米坐标系，拒绝投影结构")

    floor1_lifts = [
        item
        for item in floor1_features
        if item["feature_kind"] == "structure" and item["subtype"] == "freight_elevator"
    ]
    floor3_lifts = [
        item
        for item in floor3_features
        if item["feature_kind"] == "structure" and item["subtype"] == "freight_elevator"
    ]
    if len(floor1_lifts) != 1 or len(floor3_lifts) != 1:
        raise RuntimeError("一楼和三楼必须各有且仅有一台共同货梯")
    lift1, lift3 = floor1_lifts[0], floor3_lifts[0]
    if (
        lift1["feature_code"] != lift3["feature_code"]
        or _json(lift1["points_json"]) != _json(lift3["points_json"])
        or float(lift1["width_mm"]) != float(lift3["width_mm"])
        or float(lift1["storage_height_mm"]) != float(lift3["storage_height_mm"])
    ):
        raise RuntimeError("两层货梯尚未统一编号并精确重合，拒绝锁定")
    if lift1["feature_code"] != "LIFT-002":
        raise RuntimeError("共同货梯编号必须为 LIFT-002")

    source_columns = [
        item
        for item in floor3_features
        if item["feature_kind"] == "structure" and item["subtype"] == "custom_column"
    ]
    source_manual_walls = [
        item
        for item in floor3_features
        if item["feature_kind"] == "structure" and item["subtype"] == "custom_wall"
    ]
    source_dxf_walls = [
        item
        for item in _json(floor3_layout["structures_json"])
        if item.get("kind") in {"wall", "exterior_wall"}
        and len((item.get("geometry") or {}).get("points") or []) >= 2
    ]
    if not source_columns or not source_dxf_walls:
        raise RuntimeError("三楼权威柱网或墙体为空，拒绝生成一楼结构")

    generated = [
        *[_projected_column(item, floor1_layout["id"]) for item in source_columns],
        *[_projected_dxf_wall(item, floor1_layout["id"]) for item in source_dxf_walls],
        *[_projected_manual_wall(item, floor1_layout["id"]) for item in source_manual_walls],
    ]
    existing_by_code = {item["feature_code"]: item for item in floor1_features}
    create_features: list[dict] = []
    preserved_editable_walls: list[str] = []
    for feature in generated:
        current = existing_by_code.get(feature["feature_code"])
        if current is None:
            create_features.append(feature)
            continue
        if feature["subtype"] == "custom_wall":
            preserved_editable_walls.append(feature["feature_code"])
            continue
        if (
            _json(current["points_json"]) != _json(feature["points_json"])
            or float(current["width_mm"]) != float(feature["width_mm"])
            or float(current["storage_height_mm"]) != float(feature["storage_height_mm"])
            or current["status"] != feature["status"]
        ):
            raise RuntimeError(f"已投影柱 {feature['feature_code']} 与三楼权威柱网不一致")

    old_structures = _json(floor1_layout["structures_json"])
    retained_structures = [
        item for item in old_structures if item.get("kind") not in {"wall", "exterior_wall", "column"}
    ]
    removed_structure_counts = {
        kind: sum(1 for item in old_structures if item.get("kind") == kind)
        for kind in ("wall", "exterior_wall", "column")
    }
    lock_feature_ids = [
        item["id"] for item in (lift1, lift3) if item["status"] != "confirmed"
    ]
    if PROJECTION_MARKER not in warnings:
        warnings.extend(
            [
                PROJECTION_MARKER,
                "一楼承重柱按三楼已确认柱网生成并锁定；原一楼DXF柱线不再显示。",
                "一楼基础墙体按三楼实测墙体生成可编辑候选；原一楼DXF墙线不再显示。",
            ]
        )
    return {
        "floor1_layout_id": floor1_layout["id"],
        "floor3_layout_id": floor3_layout["id"],
        "retained_structures": retained_structures,
        "warnings": warnings,
        "create_features": create_features,
        "lock_feature_ids": lock_feature_ids,
        "source_columns": len(source_columns),
        "source_dxf_walls": len(source_dxf_walls),
        "source_manual_walls": len(source_manual_walls),
        "removed_structure_counts": removed_structure_counts,
        "preserved_existing_manual_walls": sorted(
            item["feature_code"]
            for item in floor1_features
            if item["feature_kind"] == "structure" and item["subtype"] == "custom_wall"
        ),
        "preserved_edited_projection_walls": sorted(preserved_editable_walls),
        "erp_inventory_writes": 0,
    }


def _row_dicts(connection: sqlite3.Connection, query: str, params=()) -> list[dict]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def load_plan(database_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{database_path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("候选数据库 quick_check 未通过")
        layouts = _row_dicts(connection, "SELECT * FROM twin_layouts ORDER BY floor_code, name")
        floor1 = [
            item
            for item in layouts
            if item["floor_code"] == "1F" and FRAME_MARKER in _json(item["warnings_json"])
        ]
        floor3 = [item for item in layouts if item["floor_code"] == "3F"]
        if len(floor1) != 1 or len(floor3) != 1:
            raise RuntimeError("无法唯一识别一楼校准布局和三楼实测布局")
        features1 = _row_dicts(
            connection,
            "SELECT * FROM twin_layout_features WHERE layout_id=? ORDER BY feature_code",
            (floor1[0]["id"],),
        )
        features3 = _row_dicts(
            connection,
            "SELECT * FROM twin_layout_features WHERE layout_id=? ORDER BY feature_code",
            (floor3[0]["id"],),
        )
        return build_plan(
            floor1_layout=floor1[0],
            floor3_layout=floor3[0],
            floor1_features=features1,
            floor3_features=features3,
        )
    finally:
        connection.close()


def apply_plan(database_path: Path, plan: dict, backup_dir: Path) -> dict:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"before-floor1-structure-projection-{stamp}.sqlite3"
    snapshot_path = backup_dir / f"floor1-structure-projection-plan-{stamp}.json"
    shutil.copy2(database_path, backup_path)
    with sqlite3.connect(backup_path) as backup:
        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("结构投影前备份校验失败")
    snapshot_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE twin_layouts SET structures_json=?, warnings_json=?, updated_at=? WHERE id=?",
            (
                json.dumps(plan["retained_structures"], ensure_ascii=False),
                json.dumps(plan["warnings"], ensure_ascii=False),
                now,
                plan["floor1_layout_id"],
            ),
        )
        for feature in plan["create_features"]:
            connection.execute(
                """
                INSERT INTO twin_layout_features (
                    id, layout_id, feature_code, name, feature_kind, subtype, points_json,
                    width_mm, direction, no_stacking, storage_mode, elevation_mm,
                    storage_height_mm, color, area_mm2, source, status, version,
                    created_at, updated_at
                ) VALUES (
                    :id, :layout_id, :feature_code, :name, :feature_kind, :subtype, :points_json,
                    :width_mm, :direction, :no_stacking, :storage_mode, :elevation_mm,
                    :storage_height_mm, :color, :area_mm2, :source, :status, :version,
                    :created_at, :updated_at
                )
                """,
                {
                    **feature,
                    "points_json": json.dumps(feature["points_json"], ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                },
            )
        for feature_id in plan["lock_feature_ids"]:
            connection.execute(
                "UPDATE twin_layout_features SET status='confirmed', version=version+1, updated_at=? WHERE id=?",
                (now, feature_id),
            )
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("结构投影写后 quick_check 未通过")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "backup_path": str(backup_path),
        "snapshot_path": str(snapshot_path),
        "created_features": len(plan["create_features"]),
        "locked_lifts": len(plan["lock_feature_ids"]),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="以三楼实测柱墙为权威生成一楼候选结构")
    value.add_argument("--database", type=Path, required=True)
    value.add_argument("--backup-dir", type=Path)
    value.add_argument("--apply", action="store_true")
    value.add_argument("--confirm", default="")
    return value


def main() -> None:
    args = parser().parse_args()
    if not args.database.is_file():
        raise SystemExit("数字孪生候选数据库不存在")
    plan = load_plan(args.database)
    report = {
        key: value
        for key, value in plan.items()
        if key not in {"retained_structures", "warnings", "create_features"}
    }
    report["mode"] = "apply" if args.apply else "dry-run"
    report["new_feature_counts"] = {
        subtype: sum(1 for item in plan["create_features"] if item["subtype"] == subtype)
        for subtype in ("custom_column", "custom_wall")
    }
    if args.apply:
        if args.confirm != CONFIRM_PHRASE:
            raise SystemExit(f'应用投影必须提供 --confirm "{CONFIRM_PHRASE}"')
        report["apply_result"] = apply_plan(
            args.database,
            plan,
            args.backup_dir or args.database.parent / "backups",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

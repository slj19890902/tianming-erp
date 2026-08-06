from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import httpx

from factory_twin.backend.dxf_parser import parse_layout_dxf


CONFIRM_PHRASE = "IMPORT 3F CANDIDATE"
RECALIBRATE_CONFIRM_PHRASE = "RECALIBRATE 3F CANDIDATE"
SOURCE_WORLD_M = {"x": -1.15, "y": -1.15, "width": 37.0, "height": 47.4}
# The scanned lower room is labelled 43.03m x 36.85m. In DXF coordinates its
# vertical room axis is X and horizontal room axis is Y.
TARGET_ROOM_MM = {"x": -2240.0, "y": -28819.0, "width": 36850.0, "height": 43030.0}
DXF_BOUNDS_MM = {"min_x": -23924.0, "min_y": -30276.0, "max_x": 34705.0, "max_y": 14691.0}
# The owner confirmed that F is in the connector between the two scanned room
# blocks. These anchors come from the actual DXF inner wall runs. In the PNG
# orientation, moving right is positive DXF Y and moving down is positive DXF X.
F_CONNECTOR_ANCHOR_MM = {"min_x": -1141.0, "min_y": -12325.0}
AE_HALL_ANCHOR_MM = {"min_x": 8879.0, "max_y": 14500.0}

ZONE_DEFS = (
    ("A2", 0, 8.7, 3.1, 14, "floor"), ("B2", 4.6, 8.7, 3, 14, "floor"),
    ("C2", 9.1, 8.7, 3.5, 14, "floor"), ("D2", 14.1, 8.7, 2.6, 14, "floor"),
    ("E2", 18.2, 8.7, 5, 14, "floor"), ("A1", 0, 24.6, 3.1, 7.43, "floor"),
    ("B1", 4.6, 24.6, 3, 10.8, "floor"), ("C1", 9.1, 24.6, 3.5, 13.7, "floor"),
    ("D1", 14.1, 24.6, 2.55, 15, "rack-floor"), ("E1", 18.15, 24.6, 5, 12, "floor"),
    ("AB2", 4.8, 0, 2.6, 1.6, "floor"), ("AB1", 4.8, 3.1, 2.6, 4.1, "floor"),
    ("E4", 18.05, .5, 5.3, 1.2, "rack-floor"), ("E3", 19.3, 3.2, 2.8, 4, "floor"),
    ("CD1", 9.1, 41.1, 6.6, 4, "floor"), ("DE1", 18.15, 38.1, 6.3, 1.2, "floor"),
    ("F1", 24.7, 16.2, 1.8, 6.5, "rack-floor"), ("F12", 26.5, 16.2, 2.9, 6.5, "temporary"),
    ("F2", 29.4, 16.2, 1.2, 6.5, "rack-only"), ("F3", 30.6, 16.2, 1.2, 6.5, "rack-only"),
    ("F34", 31.8, 15.9, 1.5, 6.8, "temporary"), ("F4", 33.3, 16.2, 1.2, 6.5, "rack-only"),
)
EXPECTED_AREA_CODES = {item[0] for item in ZONE_DEFS}


def map_point(x_m: float, y_m: float) -> list[float]:
    x_ratio = (x_m - SOURCE_WORLD_M["x"]) / SOURCE_WORLD_M["width"]
    y_ratio = (y_m - SOURCE_WORLD_M["y"]) / SOURCE_WORLD_M["height"]
    return [
        round(TARGET_ROOM_MM["x"] + x_ratio * TARGET_ROOM_MM["width"], 3),
        round(TARGET_ROOM_MM["y"] + y_ratio * TARGET_ROOM_MM["height"], 3),
    ]


def rectangle_points(x_m: float, y_m: float, width_m: float, height_m: float) -> list[list[float]]:
    return [
        map_point(x_m, y_m),
        map_point(x_m + width_m, y_m),
        map_point(x_m + width_m, y_m + height_m),
        map_point(x_m, y_m + height_m),
    ]


def _bounds(points: list[list[float]]) -> dict[str, float]:
    return {
        "min_x": min(point[0] for point in points),
        "min_y": min(point[1] for point in points),
        "max_x": max(point[0] for point in points),
        "max_y": max(point[1] for point in points),
    }


def _legacy_group_bounds(is_f_group: bool) -> dict[str, float]:
    points: list[list[float]] = []
    for code, x, y, width, height, _kind in ZONE_DEFS:
        if code.startswith("F") == is_f_group:
            points.extend(rectangle_points(x, y, width, height))
    return _bounds(points)


LEGACY_F_BOUNDS_MM = _legacy_group_bounds(True)
LEGACY_AE_BOUNDS_MM = _legacy_group_bounds(False)
F_TRANSLATION_MM = {
    "x": round(F_CONNECTOR_ANCHOR_MM["min_x"] - LEGACY_F_BOUNDS_MM["min_x"], 3),
    "y": round(F_CONNECTOR_ANCHOR_MM["min_y"] - LEGACY_F_BOUNDS_MM["min_y"], 3),
}
AE_TRANSLATION_MM = {
    "x": round(AE_HALL_ANCHOR_MM["min_x"] - LEGACY_AE_BOUNDS_MM["min_x"], 3),
    "y": round(AE_HALL_ANCHOR_MM["max_y"] - LEGACY_AE_BOUNDS_MM["max_y"], 3),
}
AE_RECALIBRATED_BOUNDS_MM = {
    "min_x": round(LEGACY_AE_BOUNDS_MM["min_x"] + AE_TRANSLATION_MM["x"], 3),
    "max_x": round(LEGACY_AE_BOUNDS_MM["max_x"] + AE_TRANSLATION_MM["x"], 3),
    "min_y": round(LEGACY_AE_BOUNDS_MM["min_y"] + AE_TRANSLATION_MM["y"], 3),
    "max_y": round(LEGACY_AE_BOUNDS_MM["max_y"] + AE_TRANSLATION_MM["y"], 3),
}


def _translate_points(points: list[list[float]], translation: dict[str, float]) -> list[list[float]]:
    return [
        [round(point[0] + translation["x"], 3), round(point[1] + translation["y"], 3)]
        for point in points
    ]


def _mirror_points_on_y(points: list[list[float]], min_y: float, max_y: float) -> list[list[float]]:
    return [[point[0], round(min_y + max_y - point[1], 3)] for point in points]


def _mirror_points_on_x(points: list[list[float]], min_x: float, max_x: float) -> list[list[float]]:
    return [[round(min_x + max_x - point[0], 3), point[1]] for point in points]


def should_create_missing_candidate(
    *, recalibrate_existing: bool, feature_code: str, restore_missing_codes: set[str]
) -> bool:
    return not recalibrate_existing or feature_code in restore_missing_codes


def build_candidate_features(*, alignment: str = "dxf_recalibrated_aisle_vertical_mirror") -> list[dict]:
    valid_alignments = {
        "legacy_room",
        "dxf_recalibrated",
        "dxf_recalibrated_mirrored",
        "dxf_recalibrated_aisle_vertical_mirror",
    }
    if alignment not in valid_alignments:
        raise ValueError(f"未知三楼套准方式：{alignment}")
    features: list[dict] = []
    for code, x, y, width, height, kind in ZONE_DEFS:
        is_rack = kind in {"rack-floor", "rack-only"}
        is_temporary = kind == "temporary"
        label = "货架区域" if is_rack else "过道临放" if is_temporary else "成品地线区域"
        points = rectangle_points(x, y, width, height)
        if alignment != "legacy_room":
            points = _translate_points(points, F_TRANSLATION_MM if code.startswith("F") else AE_TRANSLATION_MM)
        if alignment == "dxf_recalibrated_mirrored" and not code.startswith("F"):
            points = _mirror_points_on_y(
                points,
                AE_RECALIBRATED_BOUNDS_MM["min_y"],
                AE_RECALIBRATED_BOUNDS_MM["max_y"],
            )
        if alignment == "dxf_recalibrated_aisle_vertical_mirror" and not code.startswith("F"):
            points = _mirror_points_on_x(
                points,
                AE_RECALIBRATED_BOUNDS_MM["min_x"],
                AE_RECALIBRATED_BOUNDS_MM["max_x"],
            )
        alignment_label = (
            "旧ERP与实测DXF二次套准候选"
            if code.startswith("F")
            else "旧ERP与实测DXF沿主通道上下镜像校正候选"
            if alignment == "dxf_recalibrated_aisle_vertical_mirror"
            else "旧ERP与实测DXF左右镜像校正候选"
            if alignment == "dxf_recalibrated_mirrored"
            else "旧ERP与实测DXF二次套准候选"
        )
        features.append(
            {
                "feature_code": f"ZONE-3F-ERP-{code}",
                "name": f"{code} {label}（{alignment_label}）",
                "feature_kind": "zone",
                "subtype": "temporary_turnover" if is_temporary else "finished_wait_delivery",
                "points": points,
                "width_mm": None,
                "direction": None,
                "no_stacking": False,
                "storage_mode": "floor",
                "elevation_mm": 0,
                "storage_height_mm": 2600 if is_rack else 1800,
                "color": "#8b5cf6" if is_rack else "#f59e0b" if is_temporary else "#3b82f6",
                "source": "ai",
            }
        )
    aisle_start = map_point(-0.1, 22.7 + 1.9 / 2)
    aisle_end = map_point(-0.1 + 34.7, 22.7 + 1.9 / 2)
    if alignment != "legacy_room":
        recalibrated_ae_points = [
            point
            for feature in features
            if not feature["feature_code"].removeprefix("ZONE-3F-ERP-").startswith("F")
            for point in feature["points"]
        ]
        recalibrated_ae_bounds = _bounds(recalibrated_ae_points)
        aisle_start = [F_CONNECTOR_ANCHOR_MM["min_x"], round(aisle_start[1] + AE_TRANSLATION_MM["y"], 3)]
        aisle_end = [recalibrated_ae_bounds["max_x"], aisle_start[1]]
        if alignment == "dxf_recalibrated_mirrored":
            aisle_start = _mirror_points_on_y(
                [aisle_start],
                AE_RECALIBRATED_BOUNDS_MM["min_y"],
                AE_RECALIBRATED_BOUNDS_MM["max_y"],
            )[0]
            aisle_end = [aisle_end[0], aisle_start[1]]
    features.append(
        {
            "feature_code": "AISLE-3F-ERP-MAIN-001",
            "name": "3F 旧ERP主通道（待现场校正）",
            "feature_kind": "aisle",
            "subtype": "forklift",
            "points": [aisle_start, aisle_end],
            "width_mm": round(1.9 / SOURCE_WORLD_M["height"] * TARGET_ROOM_MM["height"], 3),
            "direction": "two_way",
            "no_stacking": True,
            "storage_mode": "floor",
            "elevation_mm": 0,
            "storage_height_mm": 1000,
            "color": "#22c55e",
            "source": "ai",
        }
    )
    # The upper room has no floor marking, so only its overall planning envelope
    # is shown. It intentionally does not guess a raw/semi-finished dividing line.
    features.append(
        {
            "feature_code": "ZONE-3F-RAW-SEMI-PLAN-001",
            "name": "半成品/原料共同待规划范围（现场无地线）",
            "feature_kind": "zone",
            "subtype": "temporary_turnover",
            "points": [[-23924, -30276], [-2744, -30276], [-2744, 494], [-23924, 494]],
            "width_mm": None,
            "direction": None,
            "no_stacking": False,
            "storage_mode": "floor",
            "elevation_mm": 0,
            "storage_height_mm": 1800,
            "color": "#f97316",
            "source": "ai",
        }
    )
    return features


def verify_legacy_area_source(database_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT area_code, planned_location_count FROM warehouse_areas ORDER BY area_code"
        ).fetchall()
        layout_count = connection.execute("SELECT COUNT(*) FROM floor3_location_layouts").fetchone()[0]
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    area_codes = {str(row[0]) for row in rows}
    if area_codes != EXPECTED_AREA_CODES or layout_count != 398 or integrity != "ok":
        raise RuntimeError(
            f"旧三楼来源不符合已验收基线：areas={len(area_codes)}, layouts={layout_count}, integrity={integrity}"
        )
    return {
        "area_count": len(rows),
        "planned_location_count": sum(int(row[1]) for row in rows),
        "layout_count": layout_count,
        "revision": revision,
        "integrity": integrity,
    }


def apply_candidates(args: argparse.Namespace, features: list[dict]) -> dict:
    headers = {"X-Editor-Token": args.editor_token}
    with httpx.Client(base_url=args.api_base, timeout=30) as client:
        with args.dxf.open("rb") as source:
            response = client.post(
                "/api/layouts/import-dxf",
                headers=headers,
                data={"name": args.layout_name, "floor_code": "3F"},
                files={"file": (args.dxf.name, source, "application/dxf")},
            )
        response.raise_for_status()
        layout = response.json()
        if layout["placements"]:
            raise RuntimeError("3F 实测布局意外包含设备，已停止候选并入")
        existing_by_code = {item["feature_code"]: item for item in layout["features"]}
        safe_baselines_by_code: dict[str, list[list[list[float]]]] = {}
        for alignment in ("legacy_room", "dxf_recalibrated", "dxf_recalibrated_mirrored"):
            for item in build_candidate_features(alignment=alignment):
                safe_baselines_by_code.setdefault(item["feature_code"], []).append(item["points"])
        created: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        blocked: list[str] = []
        pending_updates: list[tuple[dict, dict]] = []
        for feature in features:
            current = existing_by_code.get(feature["feature_code"])
            if current is None:
                continue
            if current["points"] == feature["points"]:
                skipped.append(feature["feature_code"])
                continue
            if not args.recalibrate_existing:
                skipped.append(feature["feature_code"])
                continue
            safe_points = safe_baselines_by_code[feature["feature_code"]]
            if current["status"] != "candidate" or current["source"] != "ai" or current["points"] not in safe_points:
                blocked.append(feature["feature_code"])
                continue
            pending_updates.append((current, feature))
        if blocked:
            raise RuntimeError(
                "以下对象已人工确认或坐标已改变，拒绝自动覆盖：" + ", ".join(sorted(blocked))
            )
        missing_preserved: list[str] = []
        restore_missing_codes = set(args.restore_missing_code or [])
        for feature in features:
            if feature["feature_code"] in existing_by_code:
                continue
            if not should_create_missing_candidate(
                recalibrate_existing=args.recalibrate_existing,
                feature_code=feature["feature_code"],
                restore_missing_codes=restore_missing_codes,
            ):
                missing_preserved.append(feature["feature_code"])
                continue
            created_response = client.post(
                f"/api/layouts/{layout['id']}/features", headers=headers, json=feature
            )
            created_response.raise_for_status()
            created.append(feature["feature_code"])
        for current, feature in pending_updates:
            update_payload = {
                "version": current["version"],
                "name": feature["name"],
                "points": feature["points"],
                "width_mm": feature["width_mm"],
                "direction": feature["direction"],
                "no_stacking": feature["no_stacking"],
                "storage_mode": feature["storage_mode"],
                "elevation_mm": feature["elevation_mm"],
                "storage_height_mm": feature["storage_height_mm"],
                "color": feature["color"],
            }
            update_response = client.patch(
                f"/api/features/{current['id']}", headers=headers, json=update_payload
            )
            update_response.raise_for_status()
            updated.append(feature["feature_code"])
        refreshed = client.get(f"/api/layouts/{layout['id']}")
        refreshed.raise_for_status()
        result = refreshed.json()
    return {
        "layout_id": result["id"],
        "layout_name": result["name"],
        "floor_code": result["floor_code"],
        "structures": len(result["structures"]),
        "equipment": len(result["placements"]),
        "racks": len(result["racks"]),
        "features": len(result["features"]),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "missing_preserved": missing_preserved,
        "violations": len(result["violations"]),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Plan or import the verified legacy 3F area overlay as twin candidates")
    value.add_argument("--dxf", type=Path, required=True)
    value.add_argument("--legacy-db", type=Path, required=True)
    value.add_argument("--api-base", default="http://127.0.0.1:8092")
    value.add_argument("--editor-token", default="local-mvp-token")
    value.add_argument("--layout-name", default="三楼实测仓库布局")
    value.add_argument("--apply", action="store_true")
    value.add_argument(
        "--recalibrate-existing",
        action="store_true",
        help="仅把仍与首次导入坐标一致的 AI 候选更新为本次 DXF 二次套准坐标",
    )
    value.add_argument(
        "--restore-missing-code",
        action="append",
        default=[],
        help="重新套准时仅恢复明确指定且当前缺失的候选编号；可重复提供",
    )
    value.add_argument("--confirm", default="")
    return value


def main() -> None:
    args = parser().parse_args()
    if not args.dxf.is_file() or not args.legacy_db.is_file():
        raise SystemExit("DXF 或旧三楼隔离数据库不存在")
    parsed = parse_layout_dxf(args.dxf, floor_code="3F")
    source = verify_legacy_area_source(args.legacy_db)
    features = build_candidate_features()
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "dxf_sha256": sha256(args.dxf.read_bytes()).hexdigest(),
        "dxf_units": parsed["source"]["input_units"],
        "dxf_bounds_mm": parsed["bounds_mm"],
        "dxf_structures": len(parsed["structures"]),
        "legacy_source": source,
        "candidate_features": len(features),
        "candidate_area_codes": sorted(EXPECTED_AREA_CODES),
        "target_room_mm": TARGET_ROOM_MM,
        "alignment": "dxf_recalibrated_aisle_vertical_mirror",
        "f_connector_anchor_mm": F_CONNECTOR_ANCHOR_MM,
        "f_translation_mm": F_TRANSLATION_MM,
        "ae_hall_anchor_mm": AE_HALL_ANCHOR_MM,
        "ae_translation_mm": AE_TRANSLATION_MM,
        "formal_locations_created": 0,
        "equipment_created": 0,
    }
    if args.apply:
        confirm_phrase = RECALIBRATE_CONFIRM_PHRASE if args.recalibrate_existing else CONFIRM_PHRASE
        if args.confirm != confirm_phrase:
            raise SystemExit(f"应用候选必须提供 --confirm \"{confirm_phrase}\"")
        report["apply_result"] = apply_candidates(args, features)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

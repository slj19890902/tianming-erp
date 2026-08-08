"""Apply the owner-confirmed 1F mold/plate and 3F D2 visual layout facts.

This script only transforms the checked-in read-only map export.  It never
connects to ERP or twin SQLite databases and never creates formal locations,
inventory, pallets or reservations.  Unmeasured mold-rack geometry remains a
movable visual candidate; measured plate/D2 dimensions are recorded exactly.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5


RECEIPT_CODE = "P1-16D-1-20260808"
D2_STANDARD_CODES = {
    "RACK-3F-D2-EAST-NORTH-001",
    "RACK-3F-D2-EAST-SOUTH-001",
    "RACK-3F-D2-WEST-NORTH-001",
    "RACK-3F-D2-WEST-SOUTH-001",
}
D2_SPECIAL_CODE = "RACK-3F-D2-SPECIAL-001"
MOLD_ZONE_CODES = {"ZONE-1F-MOLD-001", "ZONE-1F-MOLD-002"}
PLATE_ZONE_CODE = "ZONE-1F-PLATE-002"


def _floor(payload: dict[str, Any], floor_code: str) -> dict[str, Any]:
    floors = payload.get("floors") or {}
    floor = floors.get(floor_code)
    if not isinstance(floor, dict):
        raise ValueError(f"missing {floor_code} layout")
    return floor


def _features_by_code(floor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["feature_code"]: item for item in floor.get("features") or []}


def _stable_id(layout_id: str, code: str) -> str:
    return str(uuid5(UUID(layout_id), code))


def _revision(floor: dict[str, Any]) -> str:
    snapshot = {key: value for key, value in floor.items() if key != "revision"}
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _visual_rack(
    *,
    layout_id: str,
    code: str,
    name: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    depth_mm: float,
    height_mm: float,
    levels: int,
    level_heights_mm: list[float],
    rotation_deg: float,
    area_feature: dict[str, Any],
    level_usage: list[str],
    measurement_status: str,
) -> dict[str, Any]:
    return {
        "id": _stable_id(layout_id, code),
        "layout_id": layout_id,
        "rack_code": code,
        "name": name,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "z_mm": 0,
        "width_mm": width_mm,
        "depth_mm": depth_mm,
        "height_mm": height_mm,
        "levels": levels,
        "level_heights_mm": level_heights_mm,
        "cargo_rows": 3,
        "bays": 1,
        "access_side": "both",
        "min_aisle_width_mm": 1200,
        "rotation_deg": rotation_deg,
        "color": "#f59e0b" if measurement_status != "measured" else "#2563eb",
        "source": "manual",
        "status": "candidate",
        "is_locked": False,
        "version": 1,
        "area_feature_id": area_feature["id"],
        # Display grouping only.  This is deliberately not erp_area_code and
        # cannot turn the visual rack into an ERP inbound location.
        "area_code": area_feature["feature_code"],
        "measurement_status": measurement_status,
        "level_usage": level_usage,
        "formal_location_mapping": False,
    }


def _pending_wall_feature(
    *, layout_id: str, code: str, name: str, points: list[list[float]]
) -> dict[str, Any]:
    return {
        "id": _stable_id(layout_id, code),
        "layout_id": layout_id,
        "feature_code": code,
        "name": name,
        "feature_kind": "structure",
        "subtype": "pending_mold_wall_storage",
        "points": points,
        "width_mm": 450,
        "direction": None,
        "no_stacking": False,
        "storage_mode": "floor",
        "elevation_mm": 0,
        "storage_height_mm": 0,
        "color": "#f97316",
        "area_mm2": 0,
        "source": "manual",
        "status": "candidate",
        "is_locked": False,
        "version": 1,
        "erp_area_code": None,
        "formal_location_mapping": False,
        "site_note": "特别大的模板靠墙暂放；待归区，不属于模具001或002",
    }


def _upsert_by_code(items: list[dict[str, Any]], desired: dict[str, Any], code_key: str) -> None:
    for index, current in enumerate(items):
        if current.get(code_key) != desired[code_key]:
            continue
        # Preserve operator-adjusted map coordinates after first creation.
        for field in ("x_mm", "y_mm", "rotation_deg"):
            if field in current:
                desired[field] = current[field]
        desired["id"] = current.get("id") or desired["id"]
        desired["version"] = current.get("version", 0) + (current != desired)
        items[index] = desired
        return
    items.append(desired)


def build_layout(payload: dict[str, Any], *, edited_at: str) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    floor1 = _floor(result, "1F")
    floor3 = _floor(result, "3F")
    floor1_features = _features_by_code(floor1)
    required = MOLD_ZONE_CODES | {PLATE_ZONE_CODE}
    missing = sorted(required - floor1_features.keys())
    if missing:
        raise ValueError(f"missing confirmed 1F zones: {', '.join(missing)}")

    d2_racks = [rack for rack in floor3.get("racks") or [] if rack.get("rack_code", "").startswith("RACK-3F-D2-")]
    expected_d2 = D2_STANDARD_CODES | {D2_SPECIAL_CODE}
    actual_d2 = {rack["rack_code"] for rack in d2_racks}
    if actual_d2 != expected_d2:
        raise ValueError(f"unexpected D2 rack set: {sorted(actual_d2)}")
    for rack in d2_racks:
        measured = (2000, 2000) if rack["rack_code"] == D2_SPECIAL_CODE else (1800, 1100)
        desired_fields = dict(
            name=(
                "D2方形三层货架（现场实测）"
                if rack["rack_code"] == D2_SPECIAL_CODE
                else rack["name"].replace("标准货架", "三层货架（现场实测）")
            ),
            width_mm=measured[0],
            depth_mm=measured[1],
            height_mm=2600,
            levels=3,
            # Only a visual equal-height split.  It is not a measured shelf
            # height and cannot create ERP locations.
            level_heights_mm=[867, 1733],
            status="candidate",
            is_locked=False,
            area_code="D2",
            measurement_status="measured_outer_dimensions",
            shelf_height_status="equal_visual_draft_pending_measurement",
            cell_plan_status="pending_admin_configuration",
            level_usage=["下层：栈板", "上层：小型手工模切成品", "上层：小型手工模切成品"],
            formal_location_mapping=False,
        )
        changed = any(rack.get(field) != value for field, value in desired_fields.items())
        rack.update(desired_fields)
        if changed:
            rack["version"] = int(rack.get("version") or 0) + 1

    mold001 = floor1_features["ZONE-1F-MOLD-001"]
    mold002 = floor1_features["ZONE-1F-MOLD-002"]
    plate002 = floor1_features[PLATE_ZONE_CODE]
    layout_id = floor1["layout_id"]
    racks = floor1.setdefault("racks", [])
    desired_racks = [
        _visual_rack(
            layout_id=layout_id, code="RACK-1F-MOLD-002-LEFT-001", name="模具002左架（小模切机上方）",
            x_mm=-3353.6663575916955, y_mm=-13441.557022516077,
            width_mm=2200, depth_mm=1370, height_mm=3500, levels=3,
            level_heights_mm=[1300, 2400], rotation_deg=90, area_feature=mold002,
            level_usage=["底层：小模切机（不可入库）", "第二层：模板", "第三层：模板"],
            measurement_status="footprint_visual_candidate_pending_measurement",
        ),
        _visual_rack(
            layout_id=layout_id, code="RACK-1F-MOLD-002-MIDDLE-001", name="模具002中架（中模切机上方）",
            x_mm=-3274.2148167867363, y_mm=-16871.523706308042,
            width_mm=3200, depth_mm=1370, height_mm=3500, levels=2,
            level_heights_mm=[1500], rotation_deg=90, area_feature=mold002,
            level_usage=["底层：中模切机（不可入库）", "第二层：模板"],
            measurement_status="footprint_visual_candidate_pending_measurement",
        ),
        _visual_rack(
            layout_id=layout_id, code="RACK-1F-MOLD-001-RIGHT-001", name="模具001右侧三层架",
            x_mm=-3480, y_mm=-20825,
            width_mm=4000, depth_mm=1400, height_mm=3500, levels=3,
            level_heights_mm=[1200, 2400], rotation_deg=90, area_feature=mold001,
            level_usage=["第一层：大模板", "第二层：模板", "第三层：模板"],
            measurement_status="footprint_visual_candidate_pending_measurement",
        ),
        _visual_rack(
            layout_id=layout_id, code="RACK-1F-PLATE-002-001", name="挂板002两层整体架（现场实测）",
            x_mm=-9975, y_mm=-27250,
            width_mm=2000, depth_mm=1600, height_mm=3500, levels=2,
            level_heights_mm=[1750], rotation_deg=0, area_feature=plate002,
            level_usage=["第一层：挂板", "第二层：挂板"], measurement_status="measured_outer_dimensions",
        ),
    ]
    for rack in desired_racks:
        _upsert_by_code(racks, rack, "rack_code")

    pending_features = [
        _pending_wall_feature(
            layout_id=layout_id,
            code="PENDING-1F-MOLD-OVERSIZE-SOUTH-001",
            name="南墙特大模板暂放（待归区）",
            points=[[-4180, -23100], [-2780, -23100]],
        ),
        _pending_wall_feature(
            layout_id=layout_id,
            code="PENDING-1F-MOLD-OVERSIZE-WEST-001",
            name="西墙特大模板暂放（待归区）",
            points=[[-4430, -22850], [-4430, -18800]],
        ),
    ]
    features = floor1.setdefault("features", [])
    for feature in pending_features:
        _upsert_by_code(features, feature, "feature_code")

    for floor in (floor1, floor3):
        floor["layout_edited_at"] = edited_at
        floor["site_layout_receipt"] = RECEIPT_CODE
        floor["revision"] = _revision(floor)
    result["generated_at"] = edited_at
    result["site_layout_receipt"] = {
        "code": RECEIPT_CODE,
        "inventory_effect": "none",
        "formal_location_effect": "none",
        "unmeasured_geometry": "visual_candidate_only",
    }
    return result


def summarize(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    floor1_before = _floor(before, "1F")
    floor1_after = _floor(after, "1F")
    floor3_after = _floor(after, "3F")
    return {
        "receipt": RECEIPT_CODE,
        "one_floor_racks_before": len(floor1_before.get("racks") or []),
        "one_floor_racks_after": len(floor1_after.get("racks") or []),
        "pending_wall_markers": sum(
            item.get("subtype") == "pending_mold_wall_storage"
            for item in floor1_after.get("features") or []
        ),
        "d2_racks": sum(
            item.get("rack_code", "").startswith("RACK-3F-D2-")
            for item in floor3_after.get("racks") or []
        ),
        "database_connected": False,
        "inventory_written": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("static/factory_maps/twin_layout_v1.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    edited_at = datetime.now(timezone.utc).isoformat()
    result = build_layout(source, edited_at=edited_at)
    print(json.dumps({**summarize(source, result), "apply": args.apply}, ensure_ascii=False))
    if not args.apply:
        return
    output = args.output or args.input
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "written": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()

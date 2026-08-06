from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3


ERP_ZONE_PATTERN = re.compile(r"^ZONE-(?P<floor>\d+F)-ERP-(?P<area>[A-Z0-9]+)$")
ERP_ZONE_ALIASES = {
    # The owner replaced the old E4 guess with this measured delivery-surplus zone.
    "ZONE-3F-FG-001": "E4",
}
VISIBLE_STRUCTURE_KINDS = {"wall", "exterior_wall", "column", "door", "window"}


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _feature_payload(row: dict) -> dict:
    match = ERP_ZONE_PATTERN.match(str(row["feature_code"]))
    return {
        "id": row["id"],
        "feature_code": row["feature_code"],
        "name": row["name"],
        "feature_kind": row["feature_kind"],
        "subtype": row["subtype"],
        "points": _json(row["points_json"]),
        "width_mm": row["width_mm"],
        "direction": row.get("direction"),
        "no_stacking": bool(row.get("no_stacking", False)),
        "storage_mode": row["storage_mode"],
        "elevation_mm": row["elevation_mm"],
        "storage_height_mm": row["storage_height_mm"],
        "color": row["color"],
        "area_mm2": row.get("area_mm2") or 0,
        "source": row.get("source") or "manual",
        "status": row["status"],
        "is_locked": row["status"] == "confirmed"
        and row["subtype"] in {"custom_column", "freight_elevator"},
        "version": row.get("version") or 1,
        "erp_area_code": match.group("area") if match else ERP_ZONE_ALIASES.get(row["feature_code"]),
    }


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def build_export(connection: sqlite3.Connection) -> dict:
    connection.row_factory = sqlite3.Row
    layouts = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM twin_layouts WHERE source_name <> 'demo_factory.dxf' ORDER BY floor_code"
        )
    ]
    floors: dict[str, dict] = {}
    assets = []
    if _table_exists(connection, "twin_asset_templates"):
        assets = [
            {
                "id": row["id"],
                "name": row["name"],
                "category": row["category"],
                "render_type": row["render_type"],
                "image_url": row["image_url"],
                "color": row["color"],
                "default_width_mm": row["default_width_mm"],
                "default_depth_mm": row["default_depth_mm"],
                "default_height_mm": row["default_height_mm"],
            }
            for row in connection.execute(
                "SELECT * FROM twin_asset_templates ORDER BY name, id"
            )
        ]
    for layout in layouts:
        floor_code = str(layout["floor_code"]).upper()
        if floor_code in floors:
            raise RuntimeError(f"{floor_code} 存在多个正式候选布局，拒绝导出")
        structures = [
            item
            for item in _json(layout["structures_json"])
            if item.get("kind") in VISIBLE_STRUCTURE_KINDS
        ]
        features = [
            _feature_payload(dict(row))
            for row in connection.execute(
                "SELECT * FROM twin_layout_features WHERE layout_id=? AND subtype <> 'dxf_hidden' ORDER BY feature_code",
                (layout["id"],),
            )
        ]
        placements = [
            {
                "id": row["id"],
                "layout_id": row["layout_id"],
                "template_id": row.get("template_id") or "",
                "name": row["name"],
                "x_mm": row["x_mm"],
                "y_mm": row["y_mm"],
                "z_mm": row.get("z_mm") or 0,
                "width_mm": row["width_mm"],
                "depth_mm": row["depth_mm"],
                "height_mm": row["height_mm"],
                "rotation_deg": row["rotation_deg"],
                "is_confirmed": bool(row["is_confirmed"]),
                "is_locked": bool(row["is_locked"]),
                "version": row.get("version") or 1,
            }
            for raw_row in connection.execute(
                "SELECT * FROM twin_equipment_placements WHERE layout_id=? ORDER BY name, id",
                (layout["id"],),
            )
            for row in [dict(raw_row)]
        ]
        racks = [
            {
                "id": row["id"],
                "layout_id": row["layout_id"],
                "rack_code": row["rack_code"],
                "name": row["name"],
                "x_mm": row["x_mm"],
                "y_mm": row["y_mm"],
                "z_mm": row.get("z_mm") or 0,
                "width_mm": row["width_mm"],
                "depth_mm": row["depth_mm"],
                "height_mm": row["height_mm"],
                "levels": row.get("levels") or 1,
                "level_heights_mm": _json(row.get("level_heights_json") or "[]"),
                "cargo_rows": row.get("cargo_rows") or 3,
                "bays": row.get("bays") or 1,
                "access_side": row.get("access_side") or "both",
                "min_aisle_width_mm": row.get("min_aisle_width_mm") or 0,
                "rotation_deg": row["rotation_deg"],
                "color": row.get("color") or "#2563eb",
                "source": row.get("source") or "manual",
                "status": row["status"],
                "is_locked": bool(row["is_locked"]),
                "version": row.get("version") or 1,
            }
            for raw_row in connection.execute(
                "SELECT * FROM twin_rack_placements WHERE layout_id=? ORDER BY rack_code",
                (layout["id"],),
            )
            for row in [dict(raw_row)]
        ]
        pallets = [
            {
                "id": row["id"],
                "layout_id": row["layout_id"],
                "pallet_code": row["pallet_code"],
                "name": row["name"],
                "zone_id": row["zone_id"],
                "zone_code": next(
                    (
                        feature["feature_code"]
                        for feature in features
                        if feature["id"] == row["zone_id"]
                    ),
                    "",
                ),
                "x_mm": row["x_mm"],
                "y_mm": row["y_mm"],
                "z_mm": row.get("z_mm") or 0,
                "width_mm": row["width_mm"],
                "depth_mm": row["depth_mm"],
                "height_mm": row["height_mm"],
                "rotation_deg": row["rotation_deg"],
                "color": row.get("color") or "#b7793f",
                "visual_status": row.get("visual_status") or "empty",
                "status_note": row.get("status_note") or "",
                "is_simulated": bool(row.get("is_simulated", False)),
                "version": row.get("version") or 1,
                "snapped": False,
            }
            for raw_row in connection.execute(
                "SELECT * FROM twin_pallet_placements WHERE layout_id=? ORDER BY pallet_code",
                (layout["id"],),
            )
            for row in [dict(raw_row)]
        ] if _table_exists(connection, "twin_pallet_placements") else []
        floor = {
            "layout_id": layout["id"],
            "name": layout["name"],
            "floor_code": floor_code,
            "source_name": layout["source_name"],
            "source_sha256": layout.get("source_sha256") or "",
            "source_units": layout["source_units"],
            "bounds_mm": _json(layout["bounds_json"]),
            "structures": structures,
            "features": features,
            "placements": placements,
            "racks": racks,
            "pallets": pallets,
            "assets": assets,
            "warnings": _json(layout.get("warnings_json") or "[]"),
            "source_created_at": layout.get("created_at"),
            "source_updated_at": layout["updated_at"],
            "erp_area_codes": sorted(
                item["erp_area_code"] for item in features if item["erp_area_code"]
            ),
            "pallets_inventory_linked": False,
        }
        revision_source = json.dumps(floor, ensure_ascii=False, sort_keys=True).encode("utf-8")
        floor["revision"] = sha256(revision_source).hexdigest()[:16]
        floors[floor_code] = floor
    if set(floors) != {"1F", "3F"}:
        raise RuntimeError(f"必须同时导出 1F 和 3F，当前为 {sorted(floors)}")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "factory_twin_candidate_read_only_export",
        "inventory_source": "ERP inventory_lots remains authoritative; layout pallets are isolated visual targets only",
        "floors": floors,
    }


def preserve_operator_layout_edits(payload: dict, existing_path: Path) -> dict:
    """Keep ERP-map rack/policy edits when refreshing CAD-derived structures.

    The exported JSON becomes the operator-owned spatial layout after the first
    admin edit. A later DXF/database export may refresh walls and equipment, but
    must not silently restore deleted racks or erase area storage policies.
    """
    if not existing_path.is_file():
        return payload
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return payload
    for floor_code, floor in payload.get("floors", {}).items():
        previous = (existing.get("floors") or {}).get(floor_code)
        if not isinstance(previous, dict) or previous.get("layout_id") != floor.get("layout_id"):
            continue
        if previous.get("layout_edited_at"):
            floor["racks"] = list(previous.get("racks") or [])
            floor["retired_racks"] = list(previous.get("retired_racks") or [])
            floor["layout_edit_receipts"] = list(previous.get("layout_edit_receipts") or [])[-100:]
            floor["layout_edited_at"] = previous["layout_edited_at"]
        previous_features = {
            item.get("id"): item for item in previous.get("features") or [] if item.get("id")
        }
        for feature in floor.get("features") or []:
            prior = previous_features.get(feature.get("id"))
            if not prior:
                continue
            for key in ("allowed_inventory_types", "storage_layout"):
                if key in prior:
                    feature[key] = prior[key]
        floor.pop("revision", None)
        revision_source = json.dumps(floor, ensure_ascii=False, sort_keys=True).encode("utf-8")
        floor["revision"] = sha256(revision_source).hexdigest()[:16]
    return payload


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="导出ERP仓库使用的数字孪生只读平面")
    value.add_argument("--database", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main() -> None:
    args = parser().parse_args()
    if not args.database.is_file():
        raise SystemExit("数字孪生候选数据库不存在")
    connection = sqlite3.connect(f"file:{args.database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("数字孪生候选数据库 quick_check 未通过")
        payload = build_export(connection)
    finally:
        connection.close()
    payload = preserve_operator_layout_edits(payload, args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "floors": {
                    code: {
                        "structures": len(floor["structures"]),
                        "features": len(floor["features"]),
                        "erp_areas": len(floor["erp_area_codes"]),
                        "racks": len(floor["racks"]),
                        "pallets": len(floor["pallets"]),
                    }
                    for code, floor in payload["floors"].items()
                },
                "inventory_rows_written": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

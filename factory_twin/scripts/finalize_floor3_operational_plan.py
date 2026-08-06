"""Apply the owner-confirmed 3F operational layout as reversible candidates."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .optimize_floor3_layout import _request
except ImportError:  # direct `python factory_twin/scripts/...py` execution
    from optimize_floor3_layout import _request


LAYOUT_ID = "edfd9fd4-f013-41cf-b491-cd9d5dce6f52"


def _by_code(layout: dict[str, Any], code: str) -> dict[str, Any] | None:
    return next((item for item in layout["features"] if item["feature_code"] == code), None)


def _bbox(feature: dict[str, Any]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in feature["points"]]
    ys = [float(point[1]) for point in feature["points"]]
    return min(xs), min(ys), max(xs), max(ys)


def _door_clearance_points(structure: dict[str, Any], depth_mm: float = 1500) -> list[list[int]]:
    geometry = structure["geometry"]
    points = geometry.get("points") or []
    if len(points) >= 2:
        start, end = points[0], points[-1]
        dx, dy = float(end[0]) - float(start[0]), float(end[1]) - float(start[1])
        length = max(math.hypot(dx, dy), 1)
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        extension, half_depth = 250, depth_mm / 2
        a = (float(start[0]) - ux * extension, float(start[1]) - uy * extension)
        b = (float(end[0]) + ux * extension, float(end[1]) + uy * extension)
        return [
            [round(a[0] + px * half_depth), round(a[1] + py * half_depth)],
            [round(b[0] + px * half_depth), round(b[1] + py * half_depth)],
            [round(b[0] - px * half_depth), round(b[1] - py * half_depth)],
            [round(a[0] - px * half_depth), round(a[1] - py * half_depth)],
        ]
    x, y = float(geometry.get("x_mm", 0)), float(geometry.get("y_mm", 0))
    return [[round(x - 750), round(y - 750)], [round(x + 750), round(y - 750)], [round(x + 750), round(y + 750)], [round(x - 750), round(y + 750)]]


def build_summary(layout: dict[str, Any]) -> dict[str, int]:
    features = layout["features"]
    return {
        "release_zones": sum(_by_code(layout, code) is not None for code in ("ZONE-3F-FIN-001", "ZONE-3F-FIN-002")),
        "finished_storage_updates": sum(item.get("subtype") == "floor_marked_storage" for item in features),
        "shared_aisle_updates": sum(item.get("feature_kind") == "aisle" and item.get("subtype") == "pedestrian" for item in features),
        "rack_creates": max(0, 9 - len(layout.get("racks", []))),
        "no_go_creates": max(0, 8 - sum(item.get("feature_kind") == "no_go" and (item["feature_code"].startswith("NO-GO-3F-DOOR-") or item["feature_code"] == "NO-GO-3F-LIFT-002") for item in features)),
    }


def _patch_feature(api_base: str, token: str, feature: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    return _request(f"{api_base}/features/{feature['id']}", "PATCH", token, {"version": feature["version"], **changes})


def _swap_feature_codes(api_base: str, token: str, first: dict[str, Any], second: dict[str, Any], temporary: str) -> None:
    first_temp = _patch_feature(api_base, token, first, {"feature_code": temporary})
    _patch_feature(api_base, token, second, {"feature_code": first["feature_code"]})
    _patch_feature(api_base, token, first_temp, {"feature_code": second["feature_code"]})


def _rack_specs() -> list[dict[str, Any]]:
    common = {"z_mm": 0, "height_mm": 3000, "levels": 3, "min_aisle_width_mm": 1500, "color": "#2563eb", "source": "ai"}
    return [
        {**common, "rack_code": "RACK-3F-D2-SOUTH-001", "name": "D2南端1500mm货架", "x_mm": 17973, "y_mm": -18940, "width_mm": 2400, "depth_mm": 1500, "rotation_deg": 0, "bays": 2, "access_side": "north"},
        {**common, "rack_code": "RACK-3F-D2-WEST-001", "name": "D2西排1200mm货架", "x_mm": 17303, "y_mm": -13690, "width_mm": 9000, "depth_mm": 1200, "rotation_deg": 90, "bays": 6, "access_side": "south"},
        {**common, "rack_code": "RACK-3F-D2-EAST-001", "name": "D2东排1200mm货架", "x_mm": 18503, "y_mm": -13690, "width_mm": 9000, "depth_mm": 1200, "rotation_deg": 90, "bays": 6, "access_side": "north"},
        {**common, "rack_code": "RACK-3F-F1-WEST-001", "name": "F1西排1100mm货架", "x_mm": -1590, "y_mm": -9156, "width_mm": 5900, "depth_mm": 1100, "rotation_deg": 90, "bays": 4, "access_side": "south"},
        {**common, "rack_code": "RACK-3F-F1-EAST-001", "name": "F1东排1100mm货架", "x_mm": -490, "y_mm": -9156, "width_mm": 5900, "depth_mm": 1100, "rotation_deg": 90, "bays": 4, "access_side": "north"},
        {**common, "rack_code": "RACK-3F-F2-001", "name": "F2 1100mm货架", "x_mm": 2136, "y_mm": -9140, "width_mm": 5900, "depth_mm": 1100, "rotation_deg": 90, "bays": 4, "access_side": "both"},
        {**common, "rack_code": "RACK-3F-F3-001", "name": "F3 1100mm货架", "x_mm": 3247, "y_mm": -9112, "width_mm": 5900, "depth_mm": 1100, "rotation_deg": 90, "bays": 4, "access_side": "both"},
        {**common, "rack_code": "RACK-3F-F4-WEST-001", "name": "F4西排1100mm货架", "x_mm": 6981, "y_mm": -9090, "width_mm": 5800, "depth_mm": 1100, "rotation_deg": 90, "bays": 4, "access_side": "south"},
        {**common, "rack_code": "RACK-3F-F4-EAST-001", "name": "F4东排1100mm货架", "x_mm": 8081, "y_mm": -9090, "width_mm": 5800, "depth_mm": 1100, "rotation_deg": 90, "bays": 4, "access_side": "north"},
    ]


def apply_confirmed_plan(api_base: str, layout: dict[str, Any], token: str) -> None:
    layout_id = layout["id"]
    d1, d2 = _by_code(layout, "ZONE-3F-ERP-D1"), _by_code(layout, "ZONE-3F-ERP-D2")
    if d1 and d2 and d1["subtype"] == "rack_storage" and d2["subtype"] != "rack_storage":
        _swap_feature_codes(api_base, token, d1, d2, "TMP-3F-D-SWAP")

    layout = _request(f"{api_base}/layouts/{layout_id}")
    f12, f34 = _by_code(layout, "ZONE-3F-ERP-F12"), _by_code(layout, "ZONE-3F-ERP-F34")
    if f12 and f34 and _bbox(f12)[0] > _bbox(f34)[0]:
        _swap_feature_codes(api_base, token, f12, f34, "TMP-3F-F-AISLE-SWAP")

    layout = _request(f"{api_base}/layouts/{layout_id}")
    for feature in list(layout["features"]):
        code = feature["feature_code"]
        if code in {"ZONE-3F-FIN-001", "ZONE-3F-FIN-002"}:
            _request(f"{api_base}/features/{feature['id']}", "DELETE", token)
            continue
        if feature["feature_kind"] == "zone" and feature["subtype"] == "floor_marked_storage":
            label = code.removeprefix("ZONE-3F-ERP-")
            _patch_feature(api_base, token, feature, {"name": f"{label} 成品存放区（允许临时半成品/原料）", "subtype": "finished_storage", "color": "#eab308"})
            continue
        if feature["feature_kind"] == "aisle" and feature["subtype"] == "pedestrian":
            main = float(feature.get("width_mm") or 0) >= 1800
            _patch_feature(api_base, token, feature, {
                "name": "主通道（人/液压搬运车共用）" if main else "次通道（人/液压搬运车共用）",
                "subtype": "shared_main" if main else "shared_secondary",
                "width_mm": 1900 if main else 1500,
                "direction": "two_way", "no_stacking": True,
                "color": "#d97706" if main else "#eab308",
            })

    layout = _request(f"{api_base}/layouts/{layout_id}")
    names = {
        "ZONE-3F-ERP-D1": "D1 成品存放区（北侧无货架）",
        "ZONE-3F-ERP-D2": "D2 货架区（北端预留3500mm）",
        "ZONE-3F-ERP-F1": "F1 货架区（西侧，双排1100mm）",
        "ZONE-3F-ERP-F2": "F2 货架区",
        "ZONE-3F-ERP-F3": "F3 货架区",
        "ZONE-3F-ERP-F4": "F4 货架区（双排1100mm）",
        "ZONE-3F-ERP-F12": "F1/F2之间临时周转区",
        "ZONE-3F-ERP-F34": "F3/F4之间临时周转区",
    }
    for code, name in names.items():
        feature = _by_code(layout, code)
        if not feature:
            continue
        changes: dict[str, Any] = {"name": name}
        if code == "ZONE-3F-ERP-F1" and (_bbox(feature)[2] - _bbox(feature)[0]) < 2000:
            min_x, min_y, max_x, max_y = _bbox(feature)
            changes["points"] = [[-2140, min_y], [max_x, min_y], [max_x, max_y], [-2140, max_y]]
        _patch_feature(api_base, token, feature, changes)

    layout = _request(f"{api_base}/layouts/{layout_id}")
    existing_racks = {rack["rack_code"] for rack in layout.get("racks", [])}
    for rack in _rack_specs():
        if rack["rack_code"] not in existing_racks:
            _request(f"{api_base}/layouts/{layout_id}/racks", "POST", token, rack)

    layout = _request(f"{api_base}/layouts/{layout_id}")
    existing_features = {feature["feature_code"] for feature in layout["features"]}
    for structure in layout["structures"]:
        if structure["kind"] != "door" or structure["source_handle"] == "B44":
            continue
        code = f"NO-GO-3F-DOOR-{structure['source_handle']}"
        if code in existing_features:
            continue
        _request(f"{api_base}/layouts/{layout_id}/features", "POST", token, {
            "feature_code": code, "name": f"门洞 {structure['source_handle']} 开启禁放区",
            "feature_kind": "no_go", "subtype": "door_swing",
            "points": _door_clearance_points(structure), "no_stacking": True,
            "color": "#ef4444", "source": "ai",
        })
    if "NO-GO-3F-LIFT-002" not in existing_features:
        door = next(item for item in layout["structures"] if item["kind"] == "door" and item["source_handle"] == "B44")
        _request(f"{api_base}/layouts/{layout_id}/features", "POST", token, {
            "feature_code": "NO-GO-3F-LIFT-002", "name": "LIFT-002货梯口禁放区",
            "feature_kind": "no_go", "subtype": "freight_elevator",
            "points": _door_clearance_points(door, 2000), "no_stacking": True,
            "color": "#dc2626", "source": "ai",
        })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-id", default=LAYOUT_ID)
    parser.add_argument("--api-base", default="http://127.0.0.1:8092/api")
    parser.add_argument("--editor-token", default="local-mvp-token")
    parser.add_argument("--backup-dir", type=Path, default=Path("factory_twin/data/backups"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    layout = _request(f"{args.api_base}/layouts/{args.layout_id}")
    if layout["floor_code"] != "3F":
        raise SystemExit("Only the 3F candidate layout may be processed")
    print(json.dumps({**build_summary(layout), "apply": args.apply}, ensure_ascii=False))
    if not args.apply:
        return
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = args.backup_dir / f"3f-before-owner-confirmed-plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    backup.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_confirmed_plan(args.api_base, layout, args.editor_token)
    print(json.dumps({"backup": str(backup.resolve()), "applied": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()

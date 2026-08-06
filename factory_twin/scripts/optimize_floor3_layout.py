"""Create or apply a reversible semantic and column-grid candidate for the 3F twin layout."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


SEMANTIC_STYLES = {
    "delivery_surplus": ("送货余数暂存区（墙边）", "#f97316"),
    "floor_marked_storage": ("地线分区（用途待确认）", "#64748b"),
    "rack_storage": ("货架存放区", "#8b5cf6"),
    "unassigned_storage": ("待确认存放区（原成品待送范围过大）", "#94a3b8"),
}


def _center(points: list[list[float]]) -> tuple[float, float]:
    return (
        sum(float(point[0]) for point in points) / len(points),
        sum(float(point[1]) for point in points) / len(points),
    )


def _axis_targets(items: list[tuple[str, float]], tolerance_mm: float) -> dict[str, int]:
    groups: list[list[tuple[str, float]]] = []
    for item in sorted(items, key=lambda value: value[1]):
        if not groups or abs(item[1] - statistics.median(value for _, value in groups[-1])) > tolerance_mm:
            groups.append([item])
        else:
            groups[-1].append(item)
    targets: dict[str, int] = {}
    for group in groups:
        target = round(statistics.median(value for _, value in group))
        for feature_id, _ in group:
            targets[feature_id] = target
    return targets


def build_column_plan(features: list[dict[str, Any]], tolerance_mm: float = 1200) -> list[dict[str, Any]]:
    columns = [
        feature for feature in features
        if feature.get("feature_kind") == "structure" and feature.get("subtype") == "custom_column"
    ]
    centers = {feature["id"]: _center(feature["points"]) for feature in columns}
    x_targets = _axis_targets([(feature["id"], centers[feature["id"]][0]) for feature in columns], tolerance_mm)
    y_targets = _axis_targets([(feature["id"], centers[feature["id"]][1]) for feature in columns], tolerance_mm)
    updates = []
    for feature in columns:
        center_x, center_y = centers[feature["id"]]
        delta_x = x_targets[feature["id"]] - center_x
        delta_y = y_targets[feature["id"]] - center_y
        distance = math.hypot(delta_x, delta_y)
        start, end = feature["points"][0], feature["points"][-1]
        span = math.hypot(float(end[0]) - float(start[0]), float(end[1]) - float(start[1]))
        direction = 1 if float(end[0]) >= float(start[0]) else -1
        target_x = x_targets[feature["id"]]
        target_y = y_targets[feature["id"]]
        canonical = [
            [round(target_x - direction * span / 2), target_y],
            [round(target_x + direction * span / 2), target_y],
        ]
        current_ends = [[round(float(value)) for value in point] for point in (start, end)]
        if distance < 1 and canonical == current_ends:
            continue
        updates.append({
            "feature": feature,
            "payload": {"points": canonical},
            "delta_x_mm": round(delta_x),
            "delta_y_mm": round(delta_y),
            "distance_mm": round(distance),
            "orientation_normalized": abs(float(end[1]) - float(start[1])) >= 1,
        })
    return updates


def build_semantic_plan(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updates = []
    small_surplus_codes = {"ZONE-3F-FIN-003", "ZONE-3F-FIN-004", "ZONE-3F-FIN-005"}
    oversized_codes = {"ZONE-3F-FIN-001", "ZONE-3F-FIN-002"}
    for feature in features:
        if feature.get("feature_kind") != "zone" or feature.get("subtype") != "finished_wait_delivery":
            continue
        code = feature["feature_code"]
        if "货架区域" in feature.get("name", ""):
            subtype = "rack_storage"
        elif code in small_surplus_codes:
            subtype = "delivery_surplus"
        elif code in oversized_codes:
            subtype = "unassigned_storage"
        elif code.startswith("ZONE-3F-ERP-"):
            subtype = "floor_marked_storage"
        else:
            subtype = "unassigned_storage"
        base_name, color = SEMANTIC_STYLES[subtype]
        zone_label = code.removeprefix("ZONE-3F-ERP-").removeprefix("ZONE-3F-")
        updates.append({
            "feature": feature,
            "payload": {"name": f"{zone_label} {base_name}", "subtype": subtype, "color": color},
        })
    return updates


def _request(url: str, method: str = "GET", token: str | None = None, body: dict[str, Any] | None = None) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Editor-Token"] = token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, method=method, headers=headers, data=data)
    with urllib.request.urlopen(request) as response:
        return json.load(response) if response.length != 0 else None


def apply_plan(api_base: str, updates: list[dict[str, Any]], token: str) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    try:
        for update in updates:
            feature = update["feature"]
            body = {"version": feature["version"], **update["payload"]}
            result = _request(f"{api_base}/features/{feature['id']}", "PATCH", token, body)
            applied.append({"before": feature, "after": result})
        return applied
    except Exception:
        for item in reversed(applied):
            before, after = item["before"], item["after"]
            restore = {
                "version": after["version"], "name": before["name"], "subtype": before["subtype"],
                "points": before["points"], "color": before["color"], "width_mm": before["width_mm"],
                "direction": before["direction"], "no_stacking": before["no_stacking"],
                "storage_mode": before["storage_mode"], "elevation_mm": before["elevation_mm"],
                "storage_height_mm": before["storage_height_mm"],
            }
            _request(f"{api_base}/features/{before['id']}", "PATCH", token, restore)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-id", required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8092/api")
    parser.add_argument("--editor-token", default="local-mvp-token")
    parser.add_argument("--tolerance-mm", type=float, default=1200)
    parser.add_argument("--backup-dir", type=Path, default=Path("factory_twin/data/backups"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    layout = _request(f"{args.api_base}/layouts/{args.layout_id}")
    if layout["floor_code"] != "3F":
        raise SystemExit("Only a 3F layout may be processed by this task script")
    semantic = build_semantic_plan(layout["features"])
    columns = build_column_plan(layout["features"], args.tolerance_mm)
    moved = [item for item in columns if item["distance_mm"] >= 10 or item["orientation_normalized"]]
    summary = {
        "layout_id": layout["id"], "semantic_updates": len(semantic), "column_updates": len(moved),
        "max_column_move_mm": max((item["distance_mm"] for item in moved), default=0),
        "apply": args.apply,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not args.apply:
        return

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = args.backup_dir / f"3f-layout-before-semantic-column-opt-{stamp}.json"
    backup_path.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    applied = apply_plan(args.api_base, semantic + moved, args.editor_token)
    print(json.dumps({"backup": str(backup_path.resolve()), "applied": len(applied)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

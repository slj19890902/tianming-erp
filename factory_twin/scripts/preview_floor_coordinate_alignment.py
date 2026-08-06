"""Read-only preview for the shared 1F/3F coordinate frame.

The requested direction mapping is a clockwise quarter turn:
old north (+Y) -> new east (+X), old east (+X) -> new south (-Y).
This script never mutates a layout. It reports the elevator anchor translation and
the distance from every transformed 1F column to its nearest transformed 3F column.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any
from urllib.request import urlopen


def clockwise_quarter_turn(point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
    return y, -x


def points_center(points: list[list[float]]) -> tuple[float, float]:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def structure_center(geometry: dict[str, Any]) -> tuple[float, float]:
    if "points" in geometry:
        return points_center(geometry["points"])
    return float(geometry["x_mm"]), float(geometry["y_mm"])


def fetch_json(url: str) -> Any:
    with urlopen(url, timeout=10) as response:  # noqa: S310 - local tool URL only
        return json.load(response)


def layout_by_id(api_base: str, layout_id: str) -> dict[str, Any]:
    return fetch_json(f"{api_base.rstrip('/')}/api/layouts/{layout_id}")


def elevator(layout: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        feature for feature in layout["features"]
        if feature.get("feature_kind") == "structure"
        and feature.get("subtype") == "freight_elevator"
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"{layout['floor_code']} requires exactly one freight elevator; found {len(candidates)}"
        )
    return candidates[0]


def column_centers(layout: dict[str, Any]) -> list[tuple[str, tuple[float, float]]]:
    columns: list[tuple[str, tuple[float, float]]] = []
    for structure in layout.get("structures", []):
        if structure.get("kind") == "column":
            columns.append((
                structure.get("column_code") or structure["source_handle"],
                structure_center(structure["geometry"]),
            ))
    for feature in layout.get("features", []):
        if feature.get("subtype") == "custom_column":
            columns.append((feature["feature_code"], points_center(feature["points"])))
    return columns


def build_report(one: dict[str, Any], three: dict[str, Any], tolerance_mm: float) -> dict[str, Any]:
    lift_1f = elevator(one)
    lift_3f = elevator(three)
    anchor_1f = clockwise_quarter_turn(points_center(lift_1f["points"]))
    anchor_3f = clockwise_quarter_turn(points_center(lift_3f["points"]))
    translation = anchor_3f[0] - anchor_1f[0], anchor_3f[1] - anchor_1f[1]
    columns_3f = [
        (code, clockwise_quarter_turn(point))
        for code, point in column_centers(three)
    ]
    matches = []
    for code, point in column_centers(one):
        rotated = clockwise_quarter_turn(point)
        transformed = rotated[0] + translation[0], rotated[1] + translation[1]
        nearest_code, nearest_point = min(
            columns_3f,
            key=lambda item: math.dist(transformed, item[1]),
        )
        distance = math.dist(transformed, nearest_point)
        matches.append({
            "column_1f": code,
            "transformed_mm": [round(transformed[0], 3), round(transformed[1], 3)],
            "nearest_column_3f": nearest_code,
            "nearest_mm": [round(nearest_point[0], 3), round(nearest_point[1], 3)],
            "distance_mm": round(distance, 3),
            "within_tolerance": distance <= tolerance_mm,
        })
    distances = [row["distance_mm"] for row in matches]
    safe = bool(matches) and all(row["within_tolerance"] for row in matches)
    return {
        "mode": "read_only_preview",
        "direction_mapping": {"old_north": "new_east", "old_east": "new_south"},
        "transform": "(x, y) -> (y, -x)",
        "authoritative_floor": "3F",
        "floor_1f": {"layout_id": one["id"], "lift_code": lift_1f["feature_code"]},
        "floor_3f": {"layout_id": three["id"], "lift_code": lift_3f["feature_code"]},
        "lift_codes_match": lift_1f["feature_code"] == lift_3f["feature_code"],
        "one_to_three_translation_mm": [round(translation[0], 3), round(translation[1], 3)],
        "column_tolerance_mm": tolerance_mm,
        "column_distance_mm": {
            "minimum": min(distances) if distances else None,
            "mean": round(sum(distances) / len(distances), 3) if distances else None,
            "maximum": max(distances) if distances else None,
        },
        "safe_to_apply": safe and lift_1f["feature_code"] == lift_3f["feature_code"],
        "blockers": [
            reason for reason, blocked in (
                ("The 1F and 3F elevator codes do not identify one shared anchor.", lift_1f["feature_code"] != lift_3f["feature_code"]),
                ("At least one 1F column is outside the allowed distance from the 3F reference grid.", not safe),
            ) if blocked
        ],
        "column_matches": matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8092")
    parser.add_argument("--one-floor-layout", required=True)
    parser.add_argument("--three-floor-layout", required=True)
    parser.add_argument("--tolerance-mm", type=float, default=300.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(
        layout_by_id(args.api_base, args.one_floor_layout),
        layout_by_id(args.api_base, args.three_floor_layout),
        args.tolerance_mm,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["safe_to_apply"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

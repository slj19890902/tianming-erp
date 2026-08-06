"""Consolidate 3F storage blocks around the owner-confirmed aisle grid.

Dry-run is the default. ``--apply`` writes only to the isolated factory-twin
candidate API and stores a JSON snapshot before changing any feature.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .finalize_floor3_operational_plan import LAYOUT_ID, _bbox, _by_code, _patch_feature
    from .optimize_floor3_layout import _request
except ImportError:  # direct script execution
    from finalize_floor3_operational_plan import LAYOUT_ID, _bbox, _by_code, _patch_feature
    from optimize_floor3_layout import _request


# Four redundant fragments are absorbed by the larger, aisle-bounded blocks
# below. Feature codes retained on the target blocks remain stable.
LEFT_DELETE_CODES = {
    "ZONE-3F-SEMI-004",
    "ZONE-3F-SEMI-005",
    "ZONE-3F-SEMI-007",
    "ZONE-3F-SEMI-009",
}


LEFT_ZONE_TARGETS: dict[str, dict[str, Any]] = {
    "ZONE-3F-SEMI-002": {"name": "左区L1 半成品区（通道北侧）"},
    "ZONE-3F-RAW-002": {"name": "左区L2 原料区（上段）"},
    "ZONE-3F-RAW-001": {"name": "左区L3 原料区（上段）"},
    "ZONE-3F-SEMI-003": {
        "name": "左区L4 半成品区（西北整合）",
        "points": [[-23636, -8528], [-13250, -8528], [-13250, -3415], [-23636, -3415]],
    },
    "ZONE-3F-SEMI-006": {
        "name": "左区L5 半成品区（西中整合）",
        "points": [[-23636, -14987], [-13252, -14987], [-13252, -10187], [-23636, -10187]],
    },
    "ZONE-3F-RAW-003": {
        "name": "左区L6 原料区（西南整合）",
        "points": [[-23636, -25452], [-13236, -25452], [-13236, -16517], [-23636, -16517]],
    },
    "ZONE-3F-SEMI-010": {
        "name": "左区L7 半成品区（中部整合）",
        "points": [[-11737, -21861], [-7376, -21861], [-7376, -5427], [-11737, -5427]],
    },
    "ZONE-3F-RAW-004": {"name": "左区L8 原料区（南侧）"},
    "ZONE-3F-SEMI-008": {
        "name": "左区L9 半成品区（最南整合）",
        "points": [
            [-16610, -30014], [-2468, -30014], [-2468, -26483],
            [-13173, -26483], [-13173, -25452], [-16610, -25452],
        ],
    },
}


RIGHT_UPPER_CODES = (
    "ZONE-3F-ERP-A1",
    "ZONE-3F-ERP-B1",
    "ZONE-3F-ERP-C1",
    "ZONE-3F-ERP-D1",
    "ZONE-3F-ERP-E1",
)
RIGHT_LOWER_CODES = (
    "ZONE-3F-ERP-A2",
    "ZONE-3F-ERP-B2",
    "ZONE-3F-ERP-C2",
    "ZONE-3F-ERP-D2",
    "ZONE-3F-ERP-E2",
)


AISLE_TARGETS: dict[str, dict[str, Any]] = {
    # Keep the earlier 1900 mm central route west of the right-hand storage
    # field. It joins the local 1500 mm divider without being deleted.
    "AISLE-3F-PED-007": {
        "name": "中部主通道（人/液压搬运车共用）",
        "subtype": "shared_main",
        "width_mm": 1900,
        "points": [[-472, -4765], [9500, -4765]],
        "direction": "two_way",
        "no_stacking": True,
        "color": "#d97706",
    },
    # Owner-confirmed local exception: this is the visual/operational main
    # divider for the right field, while its real width is 1500 mm.
    "AISLE-3F-PED-022": {
        "name": "右区横向主分割通道（现场1500mm）",
        "subtype": "shared_secondary",
        "width_mm": 1500,
        "points": [[9500, -4760], [34000, -4760]],
        "direction": "two_way",
        "no_stacking": True,
        "color": "#d97706",
    },
}


def _rect(min_x: float, min_y: float, max_x: float, max_y: float) -> list[list[float]]:
    return [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]


def right_zone_target(feature: dict[str, Any]) -> dict[str, Any] | None:
    """Align a right-side zone to the 1500 mm divider without changing X."""
    code = feature["feature_code"]
    min_x, min_y, max_x, max_y = _bbox(feature)
    label = code.removeprefix("ZONE-3F-ERP-")
    if code in RIGHT_UPPER_CODES:
        name = f"{label} 成品存放区（主通道北侧）"
        if code == "ZONE-3F-ERP-D1":
            name = "D1 成品存放区（主通道北侧、无货架）"
        return {"name": name, "points": _rect(min_x, -4010, max_x, max_y)}
    if code in RIGHT_LOWER_CODES:
        name = f"{label} 成品存放区（主通道南侧）"
        if code == "ZONE-3F-ERP-D2":
            name = "D2 货架区（主通道南侧、北端预留3500mm）"
        return {"name": name, "points": _rect(min_x, min_y, max_x, -5510)}
    return None


def _normalized(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, float):
        return round(value, 3)
    return value


def _needs_change(feature: dict[str, Any], changes: dict[str, Any]) -> bool:
    return any(_normalized(feature.get(key)) != _normalized(value) for key, value in changes.items())


def build_summary(layout: dict[str, Any]) -> dict[str, int]:
    deletes = sum(_by_code(layout, code) is not None for code in LEFT_DELETE_CODES)
    left_updates = sum(
        feature is not None and _needs_change(feature, changes)
        for code, changes in LEFT_ZONE_TARGETS.items()
        if (feature := _by_code(layout, code)) is not None
    )
    right_updates = sum(
        target is not None and _needs_change(feature, target)
        for feature in layout["features"]
        if (target := right_zone_target(feature)) is not None
    )
    aisle_updates = sum(
        feature is not None and _needs_change(feature, changes)
        for code, changes in AISLE_TARGETS.items()
        if (feature := _by_code(layout, code)) is not None
    )
    return {
        "left_fragment_deletes": deletes,
        "left_block_updates": left_updates,
        "right_zone_updates": right_updates,
        "aisle_updates": aisle_updates,
    }


def apply_plan(api_base: str, layout: dict[str, Any], token: str) -> None:
    for code in LEFT_DELETE_CODES:
        feature = _by_code(layout, code)
        if feature is not None:
            _request(f"{api_base}/features/{feature['id']}", "DELETE", token)

    for code, changes in LEFT_ZONE_TARGETS.items():
        feature = _by_code(layout, code)
        if feature is not None and _needs_change(feature, changes):
            _patch_feature(api_base, token, feature, changes)

    for feature in layout["features"]:
        target = right_zone_target(feature)
        if target is not None and _needs_change(feature, target):
            _patch_feature(api_base, token, feature, target)

    for code, changes in AISLE_TARGETS.items():
        feature = _by_code(layout, code)
        if feature is not None and _needs_change(feature, changes):
            _patch_feature(api_base, token, feature, changes)


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
    backup = args.backup_dir / f"3f-before-zone-block-refine-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    backup.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_plan(args.api_base, layout, args.editor_token)
    print(json.dumps({"backup": str(backup.resolve()), "applied": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Replace long visual racks with independently movable physical modules."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .finalize_floor3_operational_plan import LAYOUT_ID
    from .optimize_floor3_layout import _request
except ImportError:
    from finalize_floor3_operational_plan import LAYOUT_ID
    from optimize_floor3_layout import _request


RACK_FIELDS = (
    "name", "x_mm", "y_mm", "z_mm", "width_mm", "depth_mm", "height_mm",
    "levels", "level_heights_mm", "cargo_rows", "bays", "access_side",
    "min_aisle_width_mm", "rotation_deg", "color",
)


def _rack(
    code: str,
    name: str,
    x_mm: float,
    y_mm: float,
    *,
    width_mm: float = 2800,
    depth_mm: float = 1100,
    height_mm: float = 2200,
    level_heights_mm: list[float] | None = None,
    cargo_rows: int = 4,
    access_side: str = "both",
) -> dict[str, Any]:
    return {
        "rack_code": code,
        "name": name,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "z_mm": 0,
        "width_mm": width_mm,
        "depth_mm": depth_mm,
        "height_mm": height_mm,
        "levels": 3,
        "level_heights_mm": level_heights_mm or [750, 1500],
        "cargo_rows": cargo_rows,
        "bays": 1,
        "access_side": access_side,
        "min_aisle_width_mm": 1500,
        "rotation_deg": 90,
        "color": "#2563eb",
        "source": "ai",
    }


def rack_specs() -> list[dict[str, Any]]:
    specs = [
        _rack(
            "RACK-3F-D2-SPECIAL-001", "D2方形三层货架（现场实测）", 17845, -17485,
            width_mm=2000, depth_mm=2000, height_mm=2600,
            level_heights_mm=[867, 1733], access_side="north",
        ),
        _rack(
            "RACK-3F-D2-WEST-SOUTH-001", "D2西排南侧三层货架（现场实测）", 17239, -14463,
            width_mm=1800, depth_mm=1100, height_mm=2600,
            level_heights_mm=[867, 1733], access_side="west",
        ),
        _rack(
            "RACK-3F-D2-WEST-NORTH-001", "D2西排北侧三层货架（现场实测）", 17247, -10561,
            width_mm=1800, depth_mm=1100, height_mm=2600,
            level_heights_mm=[867, 1733], access_side="west",
        ),
        _rack(
            "RACK-3F-D2-EAST-SOUTH-001", "D2东排南侧三层货架（现场实测）", 18403, -14423,
            width_mm=1800, depth_mm=1100, height_mm=2600,
            level_heights_mm=[867, 1733], access_side="east",
        ),
        _rack(
            "RACK-3F-D2-EAST-NORTH-001", "D2东排北侧三层货架（现场实测）", 18459, -10585,
            width_mm=1800, depth_mm=1100, height_mm=2600,
            level_heights_mm=[867, 1733], access_side="east",
        ),
    ]
    f_rows = {
        "F1": [(-1590, "WEST", "west"), (-490, "EAST", "east")],
        "F2": [(2136, "CENTER", "both")],
        "F3": [(3247, "CENTER", "both")],
        "F4": [(6981, "WEST", "west"), (8081, "EAST", "east")],
    }
    f_centres = {
        "F1": (-10556, -7756),
        "F2": (-10540, -7740),
        "F3": (-10512, -7712),
        "F4": (-10490, -7690),
    }
    for area, rows in f_rows.items():
        for x_mm, row_name, access_side in rows:
            for segment, y_mm in zip(("SOUTH", "NORTH"), f_centres[area], strict=True):
                specs.append(
                    _rack(
                        f"RACK-3F-{area}-{row_name}-{segment}-001",
                        f"{area}{'西排' if row_name == 'WEST' else '东排' if row_name == 'EAST' else ''}{'南段' if segment == 'SOUTH' else '北段'}标准货架",
                        x_mm,
                        y_mm,
                        access_side=access_side,
                    )
                )
    return specs


def _same(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(current.get(field) == desired.get(field) for field in RACK_FIELDS)


def build_summary(layout: dict[str, Any]) -> dict[str, int]:
    desired = {rack["rack_code"]: rack for rack in rack_specs()}
    current = {
        rack["rack_code"]: rack
        for rack in layout.get("racks", [])
        if rack["rack_code"].startswith(("RACK-3F-D2-", "RACK-3F-F"))
    }
    return {
        "delete_old_racks": sum(code not in desired for code in current),
        "create_modules": sum(code not in current for code in desired),
        # Existing modules belong to the operator after first creation.  Their
        # coordinates and dimensions may be tuned on site and must not be reset
        # by re-running this bootstrap script.
        "update_modules": 0,
    }


def apply_plan(api_base: str, layout: dict[str, Any], token: str) -> None:
    desired = {rack["rack_code"]: rack for rack in rack_specs()}
    current = {
        rack["rack_code"]: rack
        for rack in layout.get("racks", [])
        if rack["rack_code"].startswith(("RACK-3F-D2-", "RACK-3F-F"))
    }
    for code, rack in current.items():
        if code in desired:
            continue
        if rack.get("is_locked"):
            raise RuntimeError(f"locked rack cannot be replaced: {code}")
        _request(f"{api_base}/racks/{rack['id']}", "DELETE", token)
    for code, desired_rack in desired.items():
        current_rack = current.get(code)
        if current_rack is None:
            _request(f"{api_base}/layouts/{layout['id']}/racks", "POST", token, desired_rack)
        # Never patch an existing physical module here.  The operator owns its
        # confirmed position, dimensions, access side, shelf heights and cargo
        # rows after initial creation.


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-id", default=LAYOUT_ID)
    parser.add_argument("--api-base", default="http://127.0.0.1:8092/api")
    parser.add_argument("--editor-token", default="local-mvp-token")
    parser.add_argument("--backup-dir", type=Path, default=Path("factory_twin/data/backups"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    layout = _request(f"{args.api_base}/layouts/{args.layout_id}")
    if layout.get("floor_code") != "3F":
        raise SystemExit("Only the 3F candidate layout may be processed")
    print(json.dumps({**build_summary(layout), "apply": args.apply}, ensure_ascii=False))
    if not args.apply:
        return
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = args.backup_dir / f"3f-before-modular-racks-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    backup.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_plan(args.api_base, layout, args.editor_token)
    print(json.dumps({"backup": str(backup.resolve()), "applied": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()

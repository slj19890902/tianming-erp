from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from factory_twin.backend.dxf_parser import parse_layout_dxf


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DXF = Path(
    os.environ.get("TIANMING_FLOOR4_SOURCE_DXF", "My New Project - 4th Floor.dxf")
)
DEFAULT_LAYOUT_PATH = PROJECT_ROOT / "static" / "factory_maps" / "twin_layout_v1.json"
EXPECTED_SOURCE_SHA256 = "2824218ca929ae1640f431a93ca345d32665212ac2635a520b2deaa348d4fae8"
CANONICAL_LIFT_POINTS_MM = [[-4614.0, -1605.0], [-2614.0, -1585.0]]
CANONICAL_LIFT_WIDTH_MM = 3000.0
SITE_CALIBRATION_TARGET_POINTS = [
    {
        "point_code": "LIFT-002-CORNER-A",
        "point_mm": [-4628.999, -105.075],
        "role": "first_diagonal_lift_footprint_corner",
    },
    {
        "point_code": "LIFT-002-CORNER-C",
        "point_mm": [-2599.001, -3084.925],
        "role": "opposite_diagonal_lift_footprint_corner",
    },
    {
        "point_code": "LIFT-002-CORNER-B",
        "point_mm": [-2628.999, -85.075],
        "role": "non_collinear_lift_footprint_corner",
    },
]


def semantic_digest(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(rendered).hexdigest()


def _floor_revision(floor: dict[str, Any]) -> str:
    revision_source = {key: value for key, value in floor.items() if key != "revision"}
    return semantic_digest(revision_source)[:16]


def _lift_feature(floor: dict[str, Any], floor_code: str) -> dict[str, Any]:
    matches = [
        feature
        for feature in floor.get("features") or []
        if feature.get("feature_code") == "LIFT-002"
        and feature.get("feature_kind") == "structure"
        and feature.get("subtype") == "freight_elevator"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{floor_code} must contain exactly one canonical LIFT-002")
    return matches[0]


def canonical_lift_authority(document: dict[str, Any]) -> dict[str, Any]:
    floors = document.get("floors") or {}
    one_floor = floors.get("1F")
    three_floor = floors.get("3F")
    if not isinstance(one_floor, dict) or not isinstance(three_floor, dict):
        raise RuntimeError("the planning asset requires existing 1F and 3F floors")

    one_lift = _lift_feature(one_floor, "1F")
    three_lift = _lift_feature(three_floor, "3F")
    expected_geometry = {
        "points": CANONICAL_LIFT_POINTS_MM,
        "width_mm": CANONICAL_LIFT_WIDTH_MM,
    }
    for floor_code, lift in (("1F", one_lift), ("3F", three_lift)):
        actual_geometry = {
            "points": lift.get("points"),
            "width_mm": float(lift.get("width_mm") or 0),
        }
        if actual_geometry != expected_geometry:
            raise RuntimeError(f"{floor_code} LIFT-002 no longer matches the canonical geometry")
    if {
        "points": one_lift.get("points"),
        "width_mm": one_lift.get("width_mm"),
    } != {
        "points": three_lift.get("points"),
        "width_mm": three_lift.get("width_mm"),
    }:
        raise RuntimeError("1F and 3F must share the exact canonical LIFT-002 geometry")

    return {
        "floor_code": "3F",
        "feature_code": "LIFT-002",
        "authority_status": "confirmed_manual_locked",
        "reference_only": True,
        "materialized_on_4f": False,
        "geometry": expected_geometry,
    }


def build_floor4_scanned_plan(
    source_dxf: Path,
    document: dict[str, Any],
) -> dict[str, Any]:
    parsed = parse_layout_dxf(source_dxf, floor_code="4F")
    source = parsed["source"]
    if source["sha256"] != EXPECTED_SOURCE_SHA256:
        raise RuntimeError(
            "unexpected 4F DXF hash: "
            f"expected {EXPECTED_SOURCE_SHA256}, got {source['sha256']}"
        )
    if source["input_units"] != "m" or source["output_units"] != "mm":
        raise RuntimeError("4F source must declare metric metres and parse to millimetres")

    authority = canonical_lift_authority(document)
    structures = deepcopy(parsed["structures"])
    if any(
        "LIFT" in str(item.get("id") or "").upper()
        or "LIFT" in str(item.get("layer") or "").upper()
        or "ELEVATOR" in str(item.get("layer") or "").upper()
        for item in structures
    ):
        raise RuntimeError("the uncalibrated 4F scan must not materialize elevator geometry")

    layout_id = str(
        uuid5(NAMESPACE_URL, f"tianming-factory-twin:4F:{EXPECTED_SOURCE_SHA256}")
    )
    floor: dict[str, Any] = {
        "layout_id": layout_id,
        "name": "四楼扫描规划图（待现场三点标定）",
        "floor_code": "4F",
        "operational_status": "planning_only",
        "source_name": source["file_name"],
        "source_sha256": source["sha256"],
        "source_units": source["input_units"],
        "coordinate_units": "mm",
        "bounds_mm": deepcopy(parsed["bounds_mm"]),
        "structures": structures,
        "features": [],
        "placements": [],
        "racks": [],
        "pallets": [],
        "assets": [],
        "warnings": [
            *parsed["warnings"],
            "四楼当前仅为扫描规划资产；完成现场三点标定前不得作为正式位置或库存地图。",
            "3F/LIFT-002 仅作为坐标权威引用；本资产未生成四楼货梯几何。",
            "本资产未生成区域、货架、栈板、库存或正式库位。",
        ],
        "erp_area_codes": [],
        "pallets_inventory_linked": False,
        "layout_edit_receipts": [],
        "provenance": {
            "asset_kind": "scanned_planning_asset",
            "source_file_name": source["file_name"],
            "source_sha256": source["sha256"],
            "metric_units": {
                "source": source["input_units"],
                "coordinates": source["output_units"],
            },
            "parser": "factory_twin.backend.dxf_parser.parse_layout_dxf",
            "parser_backend": source["parser"],
            "entity_counts": deepcopy(parsed["entity_counts"]),
            "layer_counts": deepcopy(parsed["layer_counts"]),
            "structures_policy": "source_readonly_scan_only",
        },
        "canonical_authority": authority,
        "calibration": {
            "status": "requires_site_points",
            "method": "three_point_rigid_2d",
            "required_site_points": 3,
            "source_points": [],
            "target_points": deepcopy(SITE_CALIBRATION_TARGET_POINTS),
            "scale": 1.0,
            "mirror": False,
            "rotation_deg": None,
            "translation_mm": None,
            "applied": False,
            "constraints": {
                "scale_locked": True,
                "mirror_allowed": False,
                "site_confirmation_required": True,
            },
        },
    }
    floor["revision"] = _floor_revision(floor)
    return floor


def merge_floor4_scanned_plan(
    source_dxf: Path,
    layout_path: Path,
    *,
    check: bool = False,
) -> dict[str, Any]:
    document = json.loads(layout_path.read_text(encoding="utf-8"))
    original_protected = {
        code: deepcopy((document.get("floors") or {}).get(code)) for code in ("1F", "3F")
    }
    original_digests = {
        code: semantic_digest(floor) for code, floor in original_protected.items()
    }
    floor4 = build_floor4_scanned_plan(source_dxf, document)

    if check:
        if (document.get("floors") or {}).get("4F") != floor4:
            raise RuntimeError("4F scanned planning asset is not reproducible; regenerate it")
    else:
        document.setdefault("floors", {})["4F"] = floor4
        for code in ("1F", "3F"):
            if document["floors"].get(code) != original_protected[code]:
                raise RuntimeError(f"refusing to change protected {code} floor data")
            if semantic_digest(document["floors"][code]) != original_digests[code]:
                raise RuntimeError(f"refusing to change protected {code} floor semantics")

        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        temporary = layout_path.with_suffix(layout_path.suffix + ".tmp")
        try:
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(layout_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    refreshed = json.loads(layout_path.read_text(encoding="utf-8"))
    for code in ("1F", "3F"):
        if semantic_digest(refreshed["floors"][code]) != original_digests[code]:
            raise RuntimeError(f"protected {code} floor changed during 4F merge")
    if refreshed["floors"].get("4F") != floor4:
        raise RuntimeError("persisted 4F planning asset differs from the generated asset")

    return {
        "mode": "check" if check else "write",
        "layout_path": str(layout_path),
        "source_path": source_dxf.as_posix(),
        "source_sha256": floor4["source_sha256"],
        "floor_revision": floor4["revision"],
        "structures": len(floor4["structures"]),
        "features": len(floor4["features"]),
        "racks": len(floor4["racks"]),
        "pallets": len(floor4["pallets"]),
        "placements": len(floor4["placements"]),
        "calibration_status": floor4["calibration"]["status"],
        "protected_floor_semantic_sha256": original_digests,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Generate the uncalibrated 4F scanned planning asset without operational data"
    )
    value.add_argument("--source", type=Path, default=DEFAULT_SOURCE_DXF)
    value.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT_PATH)
    value.add_argument(
        "--check",
        action="store_true",
        help="verify that the persisted 4F asset exactly matches a fresh parse",
    )
    return value


def main() -> None:
    args = parser().parse_args()
    if not args.source.is_file():
        raise SystemExit(f"4F source DXF does not exist: {args.source}")
    if not args.layout.is_file():
        raise SystemExit(f"twin layout does not exist: {args.layout}")
    result = merge_floor4_scanned_plan(args.source, args.layout, check=args.check)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

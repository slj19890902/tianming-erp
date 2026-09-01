from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from factory_twin.scripts.import_floor4_scanned_plan import (
    CANONICAL_LIFT_POINTS_MM,
    CANONICAL_LIFT_WIDTH_MM,
    DEFAULT_SOURCE_DXF,
    EXPECTED_SOURCE_SHA256,
    SITE_CALIBRATION_TARGET_POINTS,
    build_floor4_scanned_plan,
    merge_floor4_scanned_plan,
    semantic_digest,
)


ROOT = Path(__file__).resolve().parents[1]
LAYOUT_PATH = ROOT / "static" / "factory_maps" / "twin_layout_v1.json"


def _document() -> dict:
    return json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))


def _lift(floor: dict) -> dict:
    matches = [
        feature
        for feature in floor["features"]
        if feature.get("feature_code") == "LIFT-002"
        and feature.get("subtype") == "freight_elevator"
    ]
    assert len(matches) == 1
    return matches[0]


def test_floor4_scan_is_traceable_metric_and_planning_only() -> None:
    floor = _document()["floors"]["4F"]

    assert floor["operational_status"] == "planning_only"
    assert floor["source_sha256"] == EXPECTED_SOURCE_SHA256
    assert floor["source_units"] == "m"
    assert floor["coordinate_units"] == "mm"
    assert floor["provenance"]["source_sha256"] == EXPECTED_SOURCE_SHA256
    assert "source_path" not in floor["provenance"]
    assert floor["provenance"]["metric_units"] == {
        "source": "m",
        "coordinates": "mm",
    }
    assert floor["provenance"]["parser"] == (
        "factory_twin.backend.dxf_parser.parse_layout_dxf"
    )
    assert floor["provenance"]["structures_policy"] == "source_readonly_scan_only"
    assert floor["structures"]
    assert all(item["source_readonly"] is True for item in floor["structures"])


def test_floor4_has_no_invented_operational_or_lift_geometry() -> None:
    floor = _document()["floors"]["4F"]

    for key in ("features", "placements", "racks", "pallets", "assets"):
        assert floor[key] == []
    assert floor["erp_area_codes"] == []
    assert floor["pallets_inventory_linked"] is False
    assert not any(
        "LIFT" in str(item.get("id") or "").upper()
        or "LIFT" in str(item.get("layer") or "").upper()
        or "ELEVATOR" in str(item.get("layer") or "").upper()
        for item in floor["structures"]
    )
    assert floor["canonical_authority"]["feature_code"] == "LIFT-002"
    assert floor["canonical_authority"]["reference_only"] is True
    assert floor["canonical_authority"]["materialized_on_4f"] is False


def test_floor4_requires_three_site_points_without_scale_or_mirror_guessing() -> None:
    calibration = _document()["floors"]["4F"]["calibration"]

    assert calibration["status"] == "requires_site_points"
    assert calibration["required_site_points"] == 3
    assert calibration["source_points"] == []
    assert calibration["target_points"] == SITE_CALIBRATION_TARGET_POINTS
    assert calibration["scale"] == 1.0
    assert calibration["mirror"] is False
    assert calibration["rotation_deg"] is None
    assert calibration["translation_mm"] is None
    assert calibration["applied"] is False
    assert calibration["constraints"] == {
        "scale_locked": True,
        "mirror_allowed": False,
        "site_confirmation_required": True,
    }


def test_1f_and_3f_keep_the_exact_shared_canonical_lift() -> None:
    floors = _document()["floors"]
    one = _lift(floors["1F"])
    three = _lift(floors["3F"])

    assert one["points"] == three["points"] == CANONICAL_LIFT_POINTS_MM
    assert one["width_mm"] == three["width_mm"] == CANONICAL_LIFT_WIDTH_MM
    authority = floors["4F"]["canonical_authority"]
    assert authority["floor_code"] == "3F"
    assert authority["geometry"] == {
        "points": CANONICAL_LIFT_POINTS_MM,
        "width_mm": CANONICAL_LIFT_WIDTH_MM,
    }


@pytest.mark.skipif(not DEFAULT_SOURCE_DXF.is_file(), reason="owner 4F source DXF is unavailable")
def test_floor4_generation_is_reproducible_and_preserves_1f_3f_semantics(
    tmp_path: Path,
) -> None:
    original = _document()
    protected = {
        code: semantic_digest(original["floors"][code]) for code in ("1F", "3F")
    }
    layout = tmp_path / "twin-layout.json"
    layout.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    first = merge_floor4_scanned_plan(DEFAULT_SOURCE_DXF, layout)
    first_bytes = layout.read_bytes()
    second = merge_floor4_scanned_plan(DEFAULT_SOURCE_DXF, layout)
    regenerated = json.loads(layout.read_text(encoding="utf-8"))

    assert first["source_sha256"] == second["source_sha256"] == EXPECTED_SOURCE_SHA256
    assert first["floor_revision"] == second["floor_revision"]
    assert layout.read_bytes() == first_bytes
    assert {
        code: semantic_digest(regenerated["floors"][code]) for code in ("1F", "3F")
    } == protected
    assert regenerated["floors"]["4F"] == build_floor4_scanned_plan(
        DEFAULT_SOURCE_DXF,
        deepcopy(original),
    )

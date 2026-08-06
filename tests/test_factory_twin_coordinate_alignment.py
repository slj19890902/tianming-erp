from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "factory_twin"
    / "scripts"
    / "preview_floor_coordinate_alignment.py"
)
SPEC = importlib.util.spec_from_file_location("coordinate_alignment", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_requested_direction_mapping_is_clockwise_quarter_turn():
    assert MODULE.clockwise_quarter_turn((0, 1)) == (1, 0)
    assert MODULE.clockwise_quarter_turn((1, 0)) == (0, -1)


def test_alignment_preview_fails_closed_when_lift_codes_or_columns_disagree():
    def layout(floor, lift_code, column_x):
        return {
            "id": floor,
            "floor_code": floor,
            "structures": [],
            "features": [
                {"feature_code": lift_code, "feature_kind": "structure", "subtype": "freight_elevator", "points": [[0, 0], [2, 0]]},
                {"feature_code": f"COL-{floor}-001", "feature_kind": "structure", "subtype": "custom_column", "points": [[column_x, 0], [column_x + 1, 0]]},
            ],
        }

    report = MODULE.build_report(layout("1F", "LIFT-003", 10), layout("3F", "LIFT-002", 1000), 300)
    assert report["lift_codes_match"] is False
    assert report["safe_to_apply"] is False
    assert len(report["blockers"]) == 2

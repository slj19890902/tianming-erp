"""Pure-object checks based on read-only 2026-09-11 product snapshots."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import box_type_rules
from app.services.drawing_geometry import PARAMETER_KEYS
from app.services.drawing_product_rules import (
    DrawingProductRuleError,
    slotted_parameter_candidates,
    validate_single_piece_slotted,
)


def product(**overrides):
    fields = dict(
        product_code="SATJ1TP159072", box_style="A1/0201 普通开槽箱",
        length_mm=450, width_mm=380, height_mm=100,
        splice_mode="single", pieces_per_box=1, flap_mm=30,
        report_length_mm=1690, report_width_mm=480,
        crease_left_mm=190, crease_middle_mm=100, crease_right_mm=190,
        manual_modified=1,
    )
    return SimpleNamespace(**(fields | overrides))


def test_satj_real_product_candidates_leave_slot_unknown():
    source = product()
    before = deepcopy(vars(source))
    assert slotted_parameter_candidates(source) == {
        "panel_1_mm": "450", "panel_2_mm": "380",
        "panel_3_mm": "450", "panel_4_mm": "380",
        "body_height_mm": "100", "top_flap_mm": "190",
        "bottom_flap_mm": "190", "glue_flap_mm": "30",
        "slot_width_mm": None,
    }
    assert vars(source) == before


def test_8116_half_width_is_decimal_and_manual_procurement_is_unchanged(monkeypatch):
    def forbidden_reporting_formula(*args, **kwargs):
        raise AssertionError("Drawing candidates must not call procurement formulas")

    monkeypatch.setattr(box_type_rules, "recommend_box_type", forbidden_reporting_formula)
    source = product(
        product_code="8116", length_mm=410, width_mm=395, height_mm=210,
        flap_mm=35, report_length_mm=1645, report_width_mm=610,
        crease_left_mm=200, crease_middle_mm=210, crease_right_mm=200,
    )
    before = deepcopy(vars(source))
    result = slotted_parameter_candidates(source)
    assert set(result) == set(PARAMETER_KEYS["slotted_v1"])
    assert result["top_flap_mm"] == result["bottom_flap_mm"] == "197.5"
    assert result["body_height_mm"] == "210"
    assert result["glue_flap_mm"] == "35"
    assert vars(source) == before
    assert source.crease_left_mm == source.crease_right_mm == 200


@pytest.mark.parametrize("configuration", [
    {"splice_mode": "double", "pieces_per_box": 2},
    {"splice_mode": "double", "pieces_per_box": 1},
    {"splice_mode": None},
    {"pieces_per_box": None},
    {"pieces_per_box": 2},
    {"pieces_per_box": True},
    {"box_style": "异形箱"},
])
def test_non_single_or_unknown_configuration_cannot_use_single_piece_template(configuration):
    source = product(**configuration)
    with pytest.raises(DrawingProductRuleError):
        validate_single_piece_slotted(source)
    with pytest.raises(DrawingProductRuleError):
        slotted_parameter_candidates(source)


def test_missing_dimensions_and_joint_remain_missing_without_defaults():
    result = slotted_parameter_candidates(product(width_mm=None, height_mm=None, flap_mm=None))
    assert result["panel_1_mm"] == result["panel_3_mm"] == "450"
    assert all(result[key] is None for key in (
        "panel_2_mm", "panel_4_mm", "top_flap_mm", "bottom_flap_mm",
        "body_height_mm", "glue_flap_mm", "slot_width_mm",
    ))


def test_decimal_inputs_keep_precision():
    result = slotted_parameter_candidates(product(
        length_mm=Decimal("450.25"), width_mm=Decimal("380.5"),
        height_mm=Decimal("100.75"), flap_mm=Decimal("30.5"),
    ))
    assert result["panel_1_mm"] == "450.25"
    assert result["top_flap_mm"] == "190.25"
    assert result["body_height_mm"] == "100.75"
    assert result["glue_flap_mm"] == "30.5"

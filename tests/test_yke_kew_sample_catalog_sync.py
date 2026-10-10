from __future__ import annotations

from decimal import Decimal

from scripts.admin.sync_yke_kew_sample_catalog import (
    KEEP_CURRENT,
    _cutting_mode,
    _material_base_code,
    _parse_crease,
    _parse_three_dimensions,
    desired_sample_fields,
    merge_sample_fields,
)


def _sample(**overrides):
    row = {
        "flute_type": "AB",
        "forming": "开槽",
        "printed": "是",
        "printing_colors": "红色",
        "joining": "打钉",
        "secondary_gluing": "否",
    }
    row.update(overrides)
    return row


def test_desired_sample_fields_maps_print_forming_and_joining() -> None:
    fields = desired_sample_fields(_sample())

    assert fields["flute_type"] == "AB"
    assert fields["box_category"] == "normal"
    assert fields["box_style"] == "A1/0201 普通开槽箱"
    assert fields["print_content"] == "印刷"
    assert fields["printing_colors"] == "红色"
    assert fields["production_process"] == "印刷、开槽、打钉"


def test_unknown_forming_preserves_box_configuration_and_marks_pending() -> None:
    fields = desired_sample_fields(_sample(forming="不确定"))

    assert fields["box_category"] == KEEP_CURRENT
    assert fields["box_style"] == KEEP_CURRENT
    assert "成型待确认" in fields["production_process"]


def test_merge_same_product_samples_keeps_union_of_required_processes() -> None:
    flat = _sample(
        flute_type="B",
        forming="模切",
        printed="否",
        printing_colors="无",
        joining="无需结合",
    )
    bonded = {
        **flat,
        "joining": "粘合",
        "secondary_gluing": "是",
    }

    merged, errors = merge_sample_fields(
        [flat, bonded],
        existing_process=None,
        existing_box_category="die_cut",
        existing_box_style="模切内盒",
    )

    assert errors == []
    assert merged is not None
    assert merged["production_process"] == "模切、粘合、二次粘合"


def test_material_base_code_strips_flute_notation_only() -> None:
    assert _material_base_code("DRD（A）") == "DRD"
    assert _material_base_code("VSNIV/AB") == "VSNIV"
    assert _material_base_code("D+D（A）") == "D+D"


def test_dimension_and_cutting_evidence_parsers() -> None:
    assert _parse_three_dimensions("512*418*150") == (
        Decimal("512"),
        Decimal("418"),
        Decimal("150"),
    )
    assert _parse_crease("211*150*211=572") == (211, 150, 211)
    assert _cutting_mode("开槽", "211*150*211=572") == "一开一"
    assert _cutting_mode("模切", "980*680=4") == "一开四"

from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.orders import _order_snapshot_box_configuration
from app.api.products import ProductPayload, box_type_rules_router
from app.api.quotations import ConvertPayload, _quotation_report_values
from app.services.box_type_rules import (
    BOX_TYPE_RULES,
    box_type_code,
    canonical_box_style,
    normalize_box_configuration,
    recommend_box_type,
)


def _quotation_item(box_type: str, length: int, width: int, height: int):
    return SimpleNamespace(
        box_type=box_type,
        length_mm=Decimal(length),
        width_mm=Decimal(width),
        height_mm=Decimal(height),
    )


def _product_payload(**updates) -> ProductPayload:
    values = {
        "customer_id": 1,
        "product_code": "BOX-RULE-001",
        "customer_material_code": "BOX-RULE-001",
        "product_name": "箱型规则测试",
        "box_category": "normal",
    }
    values.update(updates)
    return ProductPayload(**values)


def test_registry_has_stable_codes_and_explicit_historical_aliases() -> None:
    codes = {rule.code for rule in BOX_TYPE_RULES}
    assert {
        "a1_0201",
        "a3_set",
        "top_cover",
        "bottom_base",
        "surround_panel",
        "full_flap_carton",
        "half_slotted_carton",
        "liner",
        "divider",
        "die_cut_partition",
        "die_cut_inner_box",
        "irregular",
        "other",
    }.issubset(codes)
    assert canonical_box_style("围套") == "围板"
    assert canonical_box_style("全搭盖箱") == "满摇盖纸箱"
    assert canonical_box_style("平卡") == "模切内盒"
    assert box_type_code(" WCX1 五层箱1 ") == "a1_0201"
    assert box_type_code("A356客户自定义") is None


@pytest.mark.parametrize(
    ("legacy_name", "expected_code", "expected_display_name"),
    [
        ("WC 五层钉箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("WCZX 五层粘箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("SCX 单瓦箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("QCX 七层纸箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("001 思展钉箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("008 外箱无钉", "a1_0201", "A1/0201 普通开槽箱"),
        ("006 华元外箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("WZX 外纸箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("NZX 内纸箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("THWX 腾华外箱", "a1_0201", "A1/0201 普通开槽箱"),
        ("TDG 天地盖", "a3_set", "A3 天地盖"),
        ("TDGG 天地盖盖", "top_cover", "独立天盖"),
        ("TDGD 天地盖底", "bottom_base", "独立底"),
        ("WB 围板", "surround_panel", "围板"),
        ("MYG 满摇盖", "full_flap_carton", "满摇盖纸箱"),
        ("010 单瓦满摇盖", "full_flap_carton", "满摇盖纸箱"),
        ("BJX 半截箱", "half_slotted_carton", "半开槽箱"),
        ("QCB 七层板", "liner", "衬板"),
        ("FJH 飞机盒", "die_cut_inner_box", "模切内盒"),
        ("MQXX 模切小箱", "die_cut_inner_box", "模切内盒"),
        ("YXX 异型箱", "irregular", "异形箱"),
    ],
)
def test_confirmed_legacy_box_names_resolve_to_existing_types(
    legacy_name: str,
    expected_code: str,
    expected_display_name: str,
) -> None:
    assert box_type_code(legacy_name) == expected_code
    assert canonical_box_style(legacy_name) == expected_display_name


@pytest.mark.parametrize(
    "ambiguous_name",
    [
        "客户新内盒",
        "恒鹏新模切",
        "新无盖箱",
    ],
)
def test_ambiguous_legacy_names_remain_unclassified(ambiguous_name: str) -> None:
    assert box_type_code(ambiguous_name) is None


def test_registry_aliases_are_unique_after_normalization() -> None:
    seen: dict[str, str] = {}
    for rule in BOX_TYPE_RULES:
        for alias in (rule.code, rule.display_name, *rule.aliases):
            key = "".join(alias.strip().upper().split())
            assert key not in seen or seen[key] == rule.code
            seen[key] = rule.code


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    [
        ("WC 五层钉箱", "A1/0201 普通开槽箱"),
        ("TDG 天地盖", "A3 天地盖"),
        ("TDGG 天地盖盖", "独立天盖"),
        ("TDGD 天地盖底", "独立底"),
        ("WB 围板", "围板"),
        ("MYG 满摇盖", "满摇盖纸箱"),
        ("BJX 半截箱", "半开槽箱"),
        ("QCB 七层板", "衬板"),
        ("FJH 飞机盒", "模切内盒"),
        ("YXX 异型箱", "异形箱"),
    ],
)
def test_confirmed_legacy_alias_uses_exact_existing_formula_contract(
    legacy_name: str,
    canonical_name: str,
) -> None:
    values = {
        "length_mm": 400,
        "width_mm": 300,
        "height_mm": 200,
        "splice_mode": "single",
        "flap_mm": 30,
        "crease_type": "净料" if canonical_name == "衬板" else None,
    }

    legacy = recommend_box_type(box_style=legacy_name, **values)
    canonical = recommend_box_type(box_style=canonical_name, **values)

    assert legacy == canonical


def test_die_cut_inner_box_and_irregular_box_require_three_dimensions() -> None:
    rules = {rule.code: rule for rule in BOX_TYPE_RULES}
    expected = ("length_mm", "width_mm", "height_mm")

    assert rules["die_cut_inner_box"].required_dimensions == expected
    assert rules["irregular"].required_dimensions == expected
    assert rules["liner"].required_dimensions == ("length_mm", "width_mm")
    assert rules["divider"].required_dimensions == ("length_mm", "width_mm")
    assert rules["die_cut_partition"].required_dimensions == (
        "length_mm",
        "width_mm",
    )


def test_stable_rule_router_exposes_read_and_preview_paths() -> None:
    methods_by_path = {
        route.path: route.methods
        for route in box_type_rules_router.routes
    }
    assert methods_by_path["/box-type-rules"] == {"GET"}
    assert methods_by_path["/box-type-recommendation"] == {"POST"}


def test_a1_odd_width_golden_sample_for_single_and_double() -> None:
    single = recommend_box_type(
        box_style="A1/0201 普通开槽箱",
        length_mm=300,
        width_mm=201,
        height_mm=100,
        splice_mode="single",
        flap_mm=30,
    )
    assert single["auto_calculated"] is True
    assert (
        single["report_length_mm"],
        single["report_width_mm"],
        single["crease_left_mm"],
        single["crease_middle_mm"],
        single["crease_right_mm"],
    ) == (1032, 305, 103, 99, 103)
    assert single["pieces_per_box"] == 1

    double = recommend_box_type(
        box_style="A1",
        length_mm=300,
        width_mm=201,
        height_mm=100,
        splice_mode="double",
        flap_mm=30,
    )
    assert double["report_length_mm"] == 531
    assert double["report_width_mm"] == 305
    assert double["pieces_per_box"] == 2


def test_a1_even_width_keeps_existing_uncompensated_crease() -> None:
    result = recommend_box_type(
        box_style="0201",
        length_mm=300,
        width_mm=200,
        height_mm=150,
        flap_mm=30,
    )
    assert result["report_length_mm"] == 1030
    assert result["report_width_mm"] == 350
    assert (
        result["crease_left_mm"],
        result["crease_middle_mm"],
        result["crease_right_mm"],
    ) == (100, 150, 100)


def test_surround_and_full_flap_use_confirmed_width_and_splice_rules() -> None:
    surround = recommend_box_type(
        box_style="围套",
        length_mm=300,
        width_mm=200,
        height_mm=100,
        splice_mode="double",
        flap_mm=35,
    )
    assert surround["box_style"] == "围板"
    assert (surround["report_length_mm"], surround["report_width_mm"]) == (
        535,
        100,
    )
    assert surround["pieces_per_box"] == 2
    assert surround["manual_required"] is True
    assert surround["crease_left_mm"] is None

    full_flap = recommend_box_type(
        box_style="全搭盖箱",
        length_mm=300,
        width_mm=200,
        height_mm=100,
        splice_mode="single",
        flap_mm=30,
    )
    assert full_flap["box_style"] == "满摇盖纸箱"
    assert (full_flap["report_length_mm"], full_flap["report_width_mm"]) == (
        1030,
        700,
    )
    assert (
        full_flap["crease_left_mm"],
        full_flap["crease_middle_mm"],
        full_flap["crease_right_mm"],
    ) == (300, 100, 300)


def test_half_slotted_odd_width_uses_one_upward_rounded_half_flap() -> None:
    even = recommend_box_type(
        box_style="半开槽箱",
        length_mm=300,
        width_mm=200,
        height_mm=100,
        splice_mode="double",
        flap_mm=30,
    )
    assert (even["report_length_mm"], even["report_width_mm"]) == (530, 200)
    assert (
        even["crease_left_mm"],
        even["crease_middle_mm"],
        even["crease_right_mm"],
    ) == (100, 100, 0)
    assert even["pieces_per_box"] == 2

    odd_single = recommend_box_type(
        box_style="半开槽",
        length_mm=300,
        width_mm=201,
        height_mm=100,
        splice_mode="single",
        flap_mm=30,
    )
    assert odd_single["auto_calculated"] is True
    assert odd_single["manual_required"] is False
    assert (
        odd_single["report_length_mm"],
        odd_single["report_width_mm"],
        odd_single["crease_left_mm"],
        odd_single["crease_middle_mm"],
        odd_single["crease_right_mm"],
        odd_single["pieces_per_box"],
    ) == (1032, 201, 101, 100, 0, 1)

    odd_double = recommend_box_type(
        box_style="半开槽箱",
        length_mm=300,
        width_mm=201,
        height_mm=100,
        splice_mode="double",
        flap_mm=30,
    )
    assert (
        odd_double["report_length_mm"],
        odd_double["report_width_mm"],
        odd_double["crease_left_mm"],
        odd_double["crease_middle_mm"],
        odd_double["crease_right_mm"],
        odd_double["pieces_per_box"],
    ) == (531, 201, 101, 100, 0, 2)


def test_liner_can_use_length_width_only_and_never_gets_flap_or_splice() -> None:
    result = recommend_box_type(
        box_style="衬板",
        length_mm=575,
        width_mm=550,
        height_mm=None,
        splice_mode="double",
        flap_mm=30,
        crease_type="毛片",
    )
    assert (result["report_length_mm"], result["report_width_mm"]) == (575, 550)
    assert result["splice_mode"] == "single"
    assert result["pieces_per_box"] == 1
    assert result["flap_mm"] is None
    assert result["crease_type"] == "毛片"
    assert result["crease_left_mm"] is None


@pytest.mark.parametrize("box_style", ["平卡", "模切内盒", "刀卡", "隔板"])
def test_cutting_types_keep_one_to_five_but_do_not_invent_report_formula(
    box_style: str,
) -> None:
    configuration = normalize_box_configuration(
        box_style=box_style,
        splice_mode="double",
        pieces_per_box=2,
        flap_mm=30,
        default_cutting_mode="一开五",
        crease_type="其他",
    )
    assert configuration["splice_mode"] == "single"
    assert configuration["pieces_per_box"] == 1
    assert configuration["flap_mm"] is None
    assert configuration["default_cutting_mode"] == "一开五"

    result = recommend_box_type(
        box_style=box_style,
        length_mm=300,
        width_mm=200,
        height_mm=100,
    )
    assert result["auto_calculated"] is False
    assert result["manual_required"] is True


def test_product_payload_normalizes_known_types_but_preserves_unknown_manual_values() -> None:
    known = _product_payload(
        box_style="围套",
        splice_mode="double",
        pieces_per_box=1,
        flap_mm=35,
    )
    assert known.box_style == "围板"
    assert known.splice_mode == "double"
    assert known.pieces_per_box == 2
    assert known.flap_mm == 35

    no_flap = _product_payload(
        box_style="平卡",
        splice_mode="double",
        pieces_per_box=2,
        flap_mm=35,
        default_cutting_mode="一开三",
        crease_type="其他",
    )
    assert no_flap.box_style == "模切内盒"
    assert no_flap.splice_mode == "single"
    assert no_flap.pieces_per_box == 1
    assert no_flap.flap_mm is None
    assert no_flap.default_cutting_mode == "一开三"

    manual_die_cut = _product_payload(
        box_style="模切内盒",
        splice_mode="double",
        pieces_per_box=2,
        flap_mm=30,
        default_cutting_mode="一开三",
        crease_type="净料",
        report_length_mm=575,
        report_width_mm=550,
    )
    assert manual_die_cut.default_cutting_mode == "一开三"
    assert manual_die_cut.report_length_mm == 575
    assert manual_die_cut.report_width_mm == 550

    unknown = _product_payload(
        box_style="历史自定义箱型",
        splice_mode="double",
        pieces_per_box=2,
        flap_mm=37,
        default_cutting_mode="一开二",
    )
    assert unknown.box_style == "历史自定义箱型"
    assert unknown.splice_mode == "double"
    assert unknown.pieces_per_box == 2
    assert unknown.flap_mm == 37
    assert unknown.default_cutting_mode == "一开二"


def test_liner_rejects_unconfirmed_crease_value_without_defaulting_to_net() -> None:
    with pytest.raises(ValidationError) as error:
        _product_payload(box_style="衬板", crease_type="其他")
    assert "衬板的压线类型仅允许：净料、毛片" in str(error.value)

    payload = _product_payload(box_style="衬板", crease_type=None)
    assert payload.crease_type is None


def test_unknown_and_unconfirmed_component_formulas_fail_closed() -> None:
    for box_style in ("历史未知箱型", "独立天盖", "独立底", "刀卡", "隔板"):
        result = recommend_box_type(
            box_style=box_style,
            length_mm=300,
            width_mm=200,
            height_mm=50,
        )
        assert result["auto_calculated"] is False
        assert result["manual_required"] is True
        assert result["report_length_mm"] is None


def test_quotation_recommendation_uses_shared_a1_rule_and_preserves_manual_values() -> None:
    automatic = _quotation_report_values(
        _quotation_item("WCX1五层箱1", 300, 201, 100),
        ConvertPayload(product_code="A1-GOLDEN", flap_mm=30),
    )
    assert (
        automatic["report_length_mm"],
        automatic["report_width_mm"],
        automatic["crease_left_mm"],
        automatic["crease_middle_mm"],
        automatic["crease_right_mm"],
    ) == (1032, 305, 103, 99, 103)

    manual = _quotation_report_values(
        _quotation_item("A1", 300, 201, 100),
        ConvertPayload(
            product_code="A1-MANUAL",
            report_length_mm=1040,
            report_width_mm=306,
            crease_type="其他",
        ),
    )
    assert manual["report_length_mm"] == 1040
    assert manual["report_width_mm"] == 306
    assert manual["crease_type"] == "其他"

    half_slotted = _quotation_report_values(
        _quotation_item("半开槽箱", 300, 201, 100),
        ConvertPayload(
            product_code="HALF-SLOTTED-GOLDEN",
            splice_mode="double",
            flap_mm=30,
        ),
    )
    assert (
        half_slotted["report_length_mm"],
        half_slotted["report_width_mm"],
        half_slotted["crease_left_mm"],
        half_slotted["crease_middle_mm"],
        half_slotted["crease_right_mm"],
        half_slotted["splice_mode"],
        half_slotted["pieces_per_box"],
    ) == (531, 201, 101, 100, 0, "double", 2)


def test_order_snapshot_clears_known_no_flap_but_preserves_unknown_history() -> None:
    no_flap = _order_snapshot_box_configuration(
        SimpleNamespace(
            box_style="平卡",
            splice_mode="double",
            pieces_per_box=2,
            flap_mm=30,
            default_cutting_mode="一开二",
            crease_type="其他",
        )
    )
    assert no_flap["code"] == "die_cut_inner_box"
    assert no_flap["splice_mode"] == "single"
    assert no_flap["pieces_per_box"] == 1
    assert no_flap["flap_mm"] is None
    assert no_flap["default_cutting_mode"] == "一开二"

    unknown = _order_snapshot_box_configuration(
        SimpleNamespace(
            box_style="历史自定义箱型",
            splice_mode="double",
            pieces_per_box=2,
            flap_mm=39,
            default_cutting_mode="一开一",
            crease_type="其他",
        )
    )
    assert unknown["recognized"] is False
    assert unknown["splice_mode"] == "double"
    assert unknown["pieces_per_box"] == 2
    assert unknown["flap_mm"] == 39

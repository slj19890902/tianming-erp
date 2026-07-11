from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.quotations import ConvertPayload, _quotation_report_values
from app.services.report_crease import crease_width_error


def _quotation_item(box_type: str, length: int, width: int, height: int):
    return SimpleNamespace(
        box_type=box_type,
        length_mm=Decimal(length),
        width_mm=Decimal(width),
        height_mm=Decimal(height),
    )


def test_shared_validator_requires_report_width_to_equal_crease_total() -> None:
    assert crease_width_error(
        label="压线",
        crease_type="压线",
        report_width_mm=456,
        left_mm=143,
        middle_mm=170,
        right_mm=143,
    ) is None
    assert "三段合计 456mm" in crease_width_error(
        label="压线",
        crease_type="压线",
        report_width_mm=461,
        left_mm=143,
        middle_mm=170,
        right_mm=143,
    )


def test_a1_quote_recommendation_has_no_hidden_five_mm() -> None:
    values = _quotation_report_values(
        _quotation_item("A1/0201 普通开槽箱", 300, 200, 150),
        ConvertPayload(product_code="A1-001"),
    )
    assert values["report_length_mm"] == 1030
    assert values["report_width_mm"] == 350
    assert (
        values["crease_left_mm"],
        values["crease_middle_mm"],
        values["crease_right_mm"],
    ) == (100, 150, 100)
    assert values["report_width_mm"] == sum(
        values[name]
        for name in ("crease_left_mm", "crease_middle_mm", "crease_right_mm")
    )


def test_a3_quote_recommends_matching_cover_and_base_crease_sets() -> None:
    values = _quotation_report_values(
        _quotation_item("A3 天地盖", 1725, 440, 310),
        ConvertPayload(product_code="A3-001"),
    )
    assert (values["report_length_mm"], values["report_width_mm"]) == (2345, 1060)
    assert (
        values["crease_left_mm"],
        values["crease_middle_mm"],
        values["crease_right_mm"],
    ) == (310, 440, 310)
    assert (values["base_report_length_mm"], values["base_report_width_mm"]) == (
        2320,
        1035,
    )
    assert (
        values["base_crease_left_mm"],
        values["base_crease_middle_mm"],
        values["base_crease_right_mm"],
    ) == (310, 415, 310)


def test_manual_quote_values_are_not_overwritten_and_mismatch_is_rejected() -> None:
    payload = ConvertPayload(
        product_code="A1-002",
        report_length_mm=900,
        report_width_mm=461,
        crease_type="压线",
        crease_left_mm=143,
        crease_middle_mm=170,
        crease_right_mm=143,
    )
    with pytest.raises(HTTPException) as error:
        _quotation_report_values(
            _quotation_item("A1/0201 普通开槽箱", 300, 200, 150),
            payload,
        )
    assert "三段合计 456mm" in str(error.value.detail)

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scripts.admin import import_yl_common_boxes as script


MANIFEST = Path(
    "outputs/019fcc60-32ce-7ac2-8df7-0e6751e4195c/"
    "YL_28条_补充导入冻结清单.json"
)
MANIFEST_SHA256 = "C75C4B7FE3C3EDFA4B9A15C132F51701E27D0B572ED2905716F282192EBB1184"


def _payload() -> dict:
    payload, actual_sha256 = script.load_manifest(MANIFEST, MANIFEST_SHA256)
    assert actual_sha256.upper() == MANIFEST_SHA256
    return payload


def test_frozen_manifest_contains_exactly_28_safe_yl_rows() -> None:
    payload = _payload()

    script._validate_manifest_shape(payload["rows"])

    assert payload["scope"]["row_count"] == 28
    assert payload["scope"]["existing_unchanged_count"] == 3
    assert payload["scope"]["excluded_not_imported_count"] == 60
    assert len({row["product_code"] for row in payload["rows"]}) == 28


def test_customer_master_is_existing_yl_customer_only() -> None:
    assert script._customer_master(_payload()) == [
        {
            "customer_code": "YL",
            "name": "苏州工业园区驿力机车科技有限公司",
            "customer_number": 131,
        }
    ]


def test_manifest_rejects_existing_protected_product_code() -> None:
    rows = copy.deepcopy(_payload()["rows"])
    rows[0]["product_code"] = "Z.001.000001"

    with pytest.raises(RuntimeError, match="既有保护记录"):
        script._validate_manifest_shape(rows)


def test_manifest_rejects_yl_price_written_as_no_tax_price() -> None:
    rows = copy.deepcopy(_payload()["rows"])
    rows[0]["sale_unit_price_no_tax"] = rows[0]["sale_unit_price"]

    with pytest.raises(RuntimeError, match="未税价"):
        script._validate_manifest_shape(rows)


def test_manifest_rejects_invalid_a1_crease_total() -> None:
    rows = copy.deepcopy(_payload()["rows"])
    a1 = next(row for row in rows if row["box_style"] == "A1/0201 普通开槽箱")
    a1["crease_left_mm"] += 1

    with pytest.raises(RuntimeError, match="压线合计"):
        script._validate_manifest_shape(rows)


def test_target_values_keep_yl_a1_structure_and_tax_semantics() -> None:
    row = next(
        row
        for row in _payload()["rows"]
        if row["box_style"] == "A1/0201 普通开槽箱"
        and row["splice_mode"] == "double"
    )

    values = script._target_product_values(row, customer_id=136)

    assert values["splice_mode"] == "double"
    assert values["pieces_per_box"] == 2
    assert values["flap_mm"] == 35
    assert values["sale_unit_price"] is not None
    assert values["sale_unit_price_no_tax"] is None


def test_die_cut_inner_box_pressure_lines_use_traceable_other_category() -> None:
    row = next(
        row
        for row in _payload()["rows"]
        if row["product_code"] == "Z.001.000130"
    )

    values = script._target_product_values(row, customer_id=136)

    assert row["crease_type"] == "压线"
    assert values["crease_type"] == "其他"
    assert values["crease_left_mm"] == 103
    assert values["crease_middle_mm"] == 220
    assert values["crease_right_mm"] == 103
    assert "源压线类型=压线" in values["remark"]

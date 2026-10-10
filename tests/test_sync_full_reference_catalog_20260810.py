from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "admin"
    / "sync_full_reference_catalog_20260810.py"
)
SPEC = spec_from_file_location("sync_full_reference_catalog_20260810", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
sync = module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


def _reference(**overrides):
    row = {
        "source_sheet": "YKE",
        "source_row": 12,
        "source_values": {"B": "型番A", "D": "80000001", "P": "B+Z/B"},
        "product_name": "型番A",
        "drawing_number": "DRAW-1",
        "product_code": "80000001",
        "source_box_type": "A",
        "production_quantity": 100,
        "reserved_loss_quantity": 3,
        "storage_location": "B4",
        "report_raw": "100*80=4",
        "report_width_mm": 100,
        "report_length_mm": 80,
        "paper_calculation_coefficient": 0.25,
        "paper_sheet_quantity": 1,
        "legacy_material_text": "B+Z/B",
        "box_category": "die_cut",
        "sale_unit_price": 2.5,
    }
    row.update(overrides)
    return row


def test_material_aliases_use_confirmed_vik_and_kiv_direction() -> None:
    assert sync._material_source_code("B+Z/B") == "VIK"
    assert sync._material_source_code("B+Z（B）") == "VIK"
    assert sync._material_source_code("Z+B/B") == "KIV"
    assert sync._material_source_code("Z+B（B）") == "KIV"
    assert sync._material_source_code("VINIV/AB") == "VINIV"
    assert sync._material_source_code("VINIV/AB D+1RC/AB") is None


def test_source_block_preserves_every_declared_excel_value_and_semantic_field() -> None:
    first = _reference()
    second = _reference(
        source_row=18,
        source_values={"B": "型番A-2", "D": "80000001", "P": "VIK"},
        product_name="型番A-2",
        legacy_material_text="VIK",
    )
    block = sync._source_block([second, first])
    match = re.fullmatch(
        re.escape(sync.SOURCE_BLOCK_START)
        + r"(.*)"
        + re.escape(sync.SOURCE_BLOCK_END),
        block,
    )
    assert match is not None
    rows = json.loads(match.group(1))
    assert [row["Excel行"] for row in rows] == [12, 18]
    assert rows[0]["列值"] == first["source_values"]
    assert rows[0]["字段口径"] == {
        "B使用型番": "型番A",
        "C图号": "DRAW-1",
        "D品目号": "80000001",
        "E箱型": "A",
        "F生产数量": 100,
        "G预留损耗数": 3,
        "H存放位置": "B4",
        "J压线或模切用纸尺寸": "100*80=4",
        "K用纸宽度": 100,
        "M用纸长度": 80,
        "N计算用纸系数": 0.25,
        "O用纸张数": 1,
        "P材质": "B+Z/B",
    }


def test_source_block_replacement_is_idempotent_and_keeps_manual_remark() -> None:
    reference = _reference()
    first = sync._merge_source_remark("人工备注", [reference])
    second = sync._merge_source_remark(first, [reference])
    assert second == first
    assert second.startswith("人工备注；")
    assert second.count(sync.SOURCE_BLOCK_START) == 1


def test_die_cut_yield_must_be_explicit_and_maps_to_one_open_four() -> None:
    reference = _reference(report_raw="388*475=4")
    assert sync._yield_is_explicit(reference) is True
    assert reference["report_raw"].endswith("=4")
    assert sync._yield_is_explicit(_reference(report_raw="388*475")) is False
    assert sync._yield_is_explicit(
        _reference(box_category="normal", report_raw="70*60*70=200")
    ) is True


def test_new_product_is_inactive_when_source_is_not_safe_for_ordering() -> None:
    reference = _reference(report_raw="388*475", sale_unit_price=None)
    reasons = sync._new_product_safety(
        [reference],
        reference,
        material=None,
        material_error="材质主档未唯一匹配",
    )
    assert "材质主档未唯一匹配" in reasons
    assert "模切用纸尺寸未明确一开几" in reasons
    assert "基础资料现行单价为空" in reasons


def test_new_product_payload_uses_source_dimensions_material_and_inactive_gate() -> None:
    reference = _reference(
        length_mm=300,
        width_mm=200,
        height_mm=100,
        default_cutting_mode="一开四",
        crease_type=None,
        crease_left_mm=None,
        crease_middle_mm=None,
        crease_right_mm=None,
        flute_type="B",
        layer_count=3,
        base_process="轧贴",
    )
    customer = SimpleNamespace(id=9, customer_code="YKE")
    material = SimpleNamespace(id=631)
    payload = sync._payload(
        customer=customer,
        references=[reference],
        canonical=reference,
        material=material,
        existing=None,
        unsafe_reasons=[],
    )
    assert payload.customer_id == 9
    assert payload.product_code == "80000001"
    assert payload.customer_material_code == "80000001"
    assert payload.material_id == 631
    assert payload.report_width_mm == 100
    assert payload.report_length_mm == 80
    assert payload.default_cutting_mode == "一开四"
    assert payload.is_active is True
    assert payload.production_process == "模切、粘贴"


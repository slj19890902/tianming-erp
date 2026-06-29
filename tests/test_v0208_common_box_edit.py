from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.products import ProductPayload


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_common_box_edit_uses_five_compact_business_rows() -> None:
    source = _source()

    markers = (
        'class="product-edit-grid"',
        'class="product-form-row product-core-row"',
        'class="product-form-row product-size-report-row"',
        'class="product-form-row product-material-row"',
        'class="product-form-row product-print-row"',
        'class="product-form-row product-final-row"',
    )
    positions = [source.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert 'class="product-form-row product-size-row"' not in source
    assert 'class="product-form-row product-report-row"' not in source
    assert 'class="product-form-row product-remark-row"' not in source


def test_common_box_dimensions_are_integer_inputs_but_price_keeps_decimals() -> None:
    source = _source()

    for field in ("length_mm", "width_mm", "height_mm"):
        assert (
            f'type="number" min="0" step="1" '
            f'v-model.number="productForm.{field}"'
        ) in source
    assert (
        'type="number" min="0" step="0.0001" '
        'v-model="productForm.sale_unit_price"'
    ) in source
    assert "normalizeProductDimensions" in source


def test_common_box_api_rejects_fractional_mm_and_accepts_other_crease_type() -> None:
    required = {
        "customer_id": 1,
        "product_code": "BOX-001",
        "customer_material_code": "BOX-001",
        "product_name": "普通箱",
        "box_category": "normal",
    }
    payload = ProductPayload(
        **required,
        length_mm=880,
        width_mm=670,
        height_mm=110,
        crease_type="其他",
    )
    assert payload.length_mm == 880
    assert payload.crease_type == "其他"

    with pytest.raises(ValidationError):
        ProductPayload(**required, length_mm=880.5)


def test_common_box_processes_are_multi_select_and_print_type_controls_drawings() -> None:
    source = _source()

    for process in ("粘贴", "打钉", "模切", "双拼", "其他"):
        assert f'value="{process}"' in source
    assert 'v-model="productForm._production_processes"' in source

    for print_type in ("无印刷", "单色印刷", "双色印刷", "多色印刷"):
        assert f'<option value="{print_type}">{print_type}</option>' in source
    assert 'v-if="productForm.print_content !== \'无印刷\'"' in source
    assert "serializeProductionProcesses" in source


def test_common_box_size_and_report_controls_share_second_row_without_a1_button() -> None:
    source = _source()

    row_start = source.index(
        'class="product-form-row product-size-report-row"'
    )
    material_row_start = source.index(
        'class="product-form-row product-material-row"'
    )
    row = source[row_start:material_row_start]

    for marker in (
        "长(mm)", "宽(mm)", "高(mm)", "报料长宽", "压线类型",
        "压线尺寸", "上摇盖", "下摇盖", "报料备注", "重新推荐",
    ):
        assert marker in row
    assert "A1 推荐计算" not in source
    assert 'placeholder="左"' not in source
    assert 'placeholder="中"' not in source
    assert 'placeholder="右"' not in source


def test_common_box_a1_board_and_crease_recommendations_use_purchase_formula() -> None:
    source = _source()

    assert "normalizeMmInteger(value)" in source
    assert "calculateBoardSizeByBoxType(boxType, length, width, height)" in source
    assert "calculateCreaseByBoxType(boxType, length, width, height, creaseType)" in source
    assert "2 * (L + W) + 30" in source
    assert "W + H + 5" in source
    assert "Math.round(W / 2)" in source
    assert 'formula_label: "按采购报料经验推荐：2×(长+宽)+30，宽+高+5"' in source
    assert 'formula_label: "按宽度一半推荐上下摇盖，高度取执行高"' in source
    assert "(L + W + 8) * 2" not in source
    assert "W + H + 4" not in source


def test_common_box_recommendation_is_automatic_but_preserves_manual_values() -> None:
    source = _source()

    for marker in (
        '"productForm.box_style"()',
        '"productForm.length_mm"()',
        '"productForm.width_mm"()',
        '"productForm.height_mm"()',
        '"productForm.material_id"()',
        '"productForm.crease_type"()',
        "autoApplyProductRecommendations",
        "markProductReportManual",
        "markProductCreaseManual",
        "shouldAutoApplyRecommendation",
        "_report_dims_manual",
        "_crease_dims_manual",
        "reapplyProductRecommendations",
    ):
        assert marker in source
    assert '@input="markProductReportManual"' in source
    assert '@input="markProductCreaseManual"' in source


def test_common_box_type_list_preserves_legacy_and_only_a1_is_automatic() -> None:
    source = _source()

    for box_type in (
        "A1/0201 普通开槽箱", "A3 天地盖", "平卡", "刀卡", "隔板",
        "围套", "半开槽箱", "全搭盖箱", "异形箱", "其他",
    ):
        assert box_type in source
    assert "原记录：" in source
    assert "isA1BoxStyle" in source
    assert "该箱型暂无自动推荐公式，请手工填写报料长宽。" in source


def test_common_box_drawing_history_shows_filename_time_latest_and_open_action() -> None:
    source = _source()

    for marker in (
        "drawingDisplayName(drawing)",
        'index===0 ? "最新" : `历史第 ${',
        "formatDateTime(drawing.uploaded_at)",
        "查看/下载",
        "暂无图纸版本",
    ):
        assert marker in source


def test_common_box_form_save_keeps_existing_fields_and_serializes_processes() -> None:
    source = _source()

    assert (
        "payload.production_process = "
        "this.serializeProductionProcesses("
    ) in source
    assert "delete payload._production_processes" in source
    assert "delete payload._production_process_legacy" in source
    assert "delete payload._material_supplier" in source
    assert "delete payload._report_dims_manual" in source
    assert "delete payload._crease_dims_manual" in source
    assert "delete payload._recommendation_message" in source
    assert "delete payload.drawings" in source
    assert "productFormSnapshot" in source


def test_common_box_fifth_row_contains_price_and_remark() -> None:
    source = _source()

    row_start = source.index('class="product-form-row product-final-row"')
    row_end = source.index(
        '<div v-else-if="modal.type === \'material\'"',
        row_start,
    )
    row = source[row_start:row_end]
    assert "默认单价" in row
    assert "备注" in row

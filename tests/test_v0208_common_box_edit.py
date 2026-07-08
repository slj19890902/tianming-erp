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


def test_common_box_dimensions_are_integer_inputs_but_price_keeps_decimals() -> None:
    source = _source()

    for field in ("length_mm", "width_mm", "height_mm", "flap_mm"):
        assert (
            f'type="number" min="0" step="1" '
            f'v-model.number="productForm.{field}"'
        ) in source or (
            f'type="number" min="1" step="1" '
            f'v-model.number="productForm.{field}"'
        ) in source
    assert (
        'type="number" min="0" step="0.0001" '
        'v-model="productForm.sale_unit_price"'
    ) in source
    assert "normalizeProductDimensions" in source


def test_common_box_api_rejects_fractional_mm_and_accepts_splice_fields() -> None:
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
        splice_mode="double",
        flap_mm=35,
    )
    assert payload.length_mm == 880
    assert payload.crease_type == "其他"
    assert payload.splice_mode == "double"
    assert payload.pieces_per_box == 2
    assert payload.flap_mm == 35

    lid_required = {
        **required,
        "product_code": "BOX-A3",
        "customer_material_code": "BOX-A3",
        "product_name": "天地盖",
    }
    lid_payload = ProductPayload(
        **lid_required,
        box_style="A3 天地盖",
        length_mm=300,
        width_mm=200,
        height_mm=50,
        splice_mode="double",
        pieces_per_box=2,
        flap_mm=30,
        base_report_length_mm=375,
        base_report_width_mm=275,
    )
    assert lid_payload.splice_mode == "single"
    assert lid_payload.pieces_per_box == 1
    assert lid_payload.flap_mm is None
    assert lid_payload.base_report_length_mm == 375
    assert lid_payload.base_report_width_mm == 275

    with pytest.raises(ValidationError):
        ProductPayload(**required, length_mm=880.5)


def test_common_box_processes_remove_double_and_print_type_controls_drawings() -> None:
    source = _source()

    for process in ("粘贴", "打钉", "模切", "其他"):
        assert f'value="{process}"' in source
    assert 'value="双拼"' not in source
    assert 'v-model="productForm._production_processes"' in source

    for print_type in ("无印刷", "单色印刷", "双色印刷", "多色印刷"):
        assert f'<option value="{print_type}">{print_type}</option>' in source
    assert 'v-if="productForm.print_content !== \'无印刷\'"' in source
    assert "serializeProductionProcesses" in source


def test_common_box_second_row_contains_splice_flap_report_and_crease() -> None:
    source = _source()

    row_start = source.index('class="product-form-row product-size-report-row"')
    material_row_start = source.index('class="product-form-row product-material-row"')
    row = source[row_start:material_row_start]

    for marker in (
        "长(mm)",
        "宽(mm)",
        "高(mm)",
        "拼箱方式",
        "单拼",
        "双拼",
        "舌头(mm)",
        "报料长宽",
        "压线类型",
        "压线尺寸",
        "报料备注",
        "重新推荐",
    ):
        assert marker in row
    assert "A1 推荐计算" not in source
    assert "<span>上摇盖</span>" not in row
    assert "<span>高</span>" not in row
    assert "<span>下摇盖</span>" not in row


def test_common_box_crease_inputs_use_placeholders_and_plus_separators() -> None:
    source = _source()

    for marker in (
        'placeholder="上摇盖"',
        'placeholder="高"',
        'placeholder="下摇盖"',
        'aria-label="上摇盖"',
        'aria-label="高"',
        'aria-label="下摇盖"',
        'class="crease-segment-plus"',
        ".crease-segments .input::placeholder",
    ):
        assert marker in source
    assert source.count('class="crease-segment-plus"') >= 2


def test_common_box_a1_board_and_crease_recommendations_use_splice_and_flap() -> None:
    source = _source()

    assert "normalizeMmInteger(value)" in source
    assert "calculateBoardSizeByBoxType(boxType, length, width, height, spliceMode, flapMm)" in source
    assert "calculateCreaseByBoxType(boxType, length, width, height, creaseType)" in source
    assert "2 * (L + W) + flap" in source
    assert "L + W + flap" in source
    assert "W + H + 5" not in source
    assert "board_width: this.normalizeMmInteger(W + H)" in source
    assert "form.report_width_mm = left + middle + right;" in source
    assert "const expectedWidth = sum;" in source
    assert "Math.round(W / 2)" in source
    assert "A1/0201 双拼推荐" in source
    assert "A1/0201 单拼推荐" in source
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
        '"productForm.splice_mode"()',
        '"productForm.flap_mm"()',
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


def test_common_box_type_list_preserves_legacy_and_supports_standard_formulas() -> None:
    source = _source()

    for box_type in (
        "A1/0201 普通开槽箱",
        "A3 天地盖",
        "平卡",
        "刀卡",
        "隔板",
        "围套",
        "半开槽箱",
        "全搭盖箱",
        "异形箱",
        "其他",
    ):
        assert box_type in source
    assert "原记录：" in source
    assert "isA1BoxStyle" in source
    assert "isTelescopingLidBoxStyle" in source


def test_telescoping_lid_edit_layout_separates_cover_and_base_rows() -> None:
    source = _source()

    assert 'class="telescoping-component-grid"' in source
    for marker in (
        "盖报料长宽",
        "底报料长宽",
        "盖压线类型",
        "底压线类型",
        "盖压线尺寸",
        "底压线尺寸",
        "底料备注",
    ):
        assert marker in source

    size_row_start = source.index('class="product-form-row product-size-report-row"')
    material_row_start = source.index('class="product-form-row product-material-row"')
    size_row = source[size_row_start:material_row_start]
    assert "telescoping-component-grid" in size_row

    print_row_start = source.index('class="product-form-row product-print-row"')
    final_row_start = source.index('class="product-form-row product-final-row"')
    print_row = source[print_row_start:final_row_start]
    assert "报料备注" in print_row
    assert "v-model.trim=\"productForm.report_notes\"" in print_row
    assert "compact-remark-field" in source
    assert "A3 天地盖推荐" in source
    assert '["平卡", "刀卡", "隔板"]' in source
    assert "围套推荐" in source
    assert "半开槽箱推荐" in source
    assert "全搭盖箱推荐" in source


def test_telescoping_lid_requisition_modal_splits_cover_and_base_lines() -> None:
    source = _source()

    assert "buildRequisitionFormLines(row)" in source
    assert 'component_type:"cover"' in source
    assert 'component_type:"base"' in source
    assert "rows.flatMap(row => this.buildRequisitionFormLines(row))" in source


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


def test_common_box_form_save_keeps_splice_and_serializes_processes() -> None:
    source = _source()
    save_block = source[source.index("async saveModal()"):source.index('if (this.modal.type === "material")')]

    assert "payload.production_process = this.serializeProductionProcesses(" in source
    assert '"pieces_per_box", "flap_mm",' in save_block
    assert '"splice_mode", "pieces_per_box", "flap_mm"' not in save_block
    assert '"base_report_length_mm", "base_report_width_mm"' in save_block
    assert "delete payload._production_processes" in source
    assert "delete payload._production_process_legacy" in source
    assert "delete payload._material_supplier" in source
    assert "delete payload._report_dims_manual" in source
    assert "delete payload._crease_dims_manual" in source
    assert "delete payload._base_report_dims_manual" in source
    assert "delete payload._base_crease_dims_manual" in source
    assert "delete payload._recommendation_message" in source
    assert "delete payload.drawings" in source
    assert "productFormSnapshot" in source


def test_common_box_fifth_row_contains_price_and_remark() -> None:
    source = _source()

    row_start = source.index('class="product-form-row product-final-row"')
    row_end = source.index('<div v-else-if="modal.type === \'material\'"', row_start)
    row = source[row_start:row_end]
    assert "默认单价" in row
    assert "备注" in row

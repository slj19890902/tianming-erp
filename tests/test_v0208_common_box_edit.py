from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_common_box_edit_uses_six_compact_business_rows() -> None:
    source = _source()

    markers = (
        'class="product-edit-grid"',
        'class="product-form-row product-core-row"',
        'class="product-form-row product-size-row"',
        'class="product-form-row product-material-row"',
        'class="product-form-row product-report-row"',
        'class="product-form-row product-print-row"',
        'class="product-form-row product-remark-row"',
    )
    positions = [source.index(marker) for marker in markers]
    assert positions == sorted(positions)


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


def test_common_box_processes_are_multi_select_and_print_type_controls_drawings() -> None:
    source = _source()

    for process in ("粘贴", "打钉", "模切", "双拼", "其他"):
        assert f'value="{process}"' in source
    assert 'v-model="productForm._production_processes"' in source

    for print_type in ("无印刷", "单色印刷", "双色印刷", "多色印刷"):
        assert f'<option value="{print_type}">{print_type}</option>' in source
    assert 'v-if="productForm.print_content !== \'无印刷\'"' in source
    assert "serializeProductionProcesses" in source


def test_common_box_report_controls_stay_in_one_row_with_a1_button() -> None:
    source = _source()

    report_row_start = source.index(
        'class="product-form-row product-report-row"'
    )
    print_row_start = source.index(
        'class="product-form-row product-print-row"'
    )
    report_block = source[report_row_start:print_row_start]

    for marker in ("报料长宽", "压线类型", "A1 推荐计算", "报料备注"):
        assert marker in report_block
    assert 'class="crease-control-line"' in report_block


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
    assert "delete payload.drawings" in source
    assert "productFormSnapshot" in source

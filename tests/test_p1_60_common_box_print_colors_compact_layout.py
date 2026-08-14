from pathlib import Path


INDEX_PATH = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _slice(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def _css_rule(source: str, selector: str) -> str:
    start = source.index(selector)
    return source[start : source.index("}", start) + 1]


def test_direct_print_status_and_all_color_inputs_share_one_compact_workbench():
    source = INDEX_PATH.read_text(encoding="utf-8")
    print_row = _slice(
        source,
        '<div class="product-form-row product-print-row">',
        '<div class="product-form-row product-final-row">',
    )
    workbench = _slice(
        print_row,
        '<div v-if="productForm.supply_mode!==\'external_purchase\'" class="product-printing-direct-workbench"',
        '<div v-if="productForm.supply_mode!==\'external_purchase\' && productHasPrinting(productForm) && productForm.printing_plate_mode===\'plate\'"',
    )

    assert 'v-model="productForm._printing_situation"' in workbench
    assert 'class="product-printing-color-config"' in workbench
    assert 'v-for="index in productDirectPrintingColorCount(productForm)"' in workbench
    assert "product-print-method-field" not in print_row
    assert "productPrintingColorSummary(productForm) || '颜色待完善'" not in print_row


def test_desktop_color_layout_is_not_forced_to_a_full_width_second_row():
    source = INDEX_PATH.read_text(encoding="utf-8")
    workbench_rule = _css_rule(source, ".product-printing-direct-workbench {")
    colors_rule = _css_rule(source, ".product-printing-color-config {")
    field_rule = _css_rule(source, ".product-printing-color-field {")

    assert "display:flex" in workbench_rule
    assert "flex-wrap:nowrap" in workbench_rule
    assert "display:flex" in colors_rule
    assert "flex:1 0 100%" not in colors_rule
    assert "min-width:0" in colors_rule
    assert "min-width:0" in field_rule
    assert "max-width" in field_rule


def test_color_inputs_keep_long_names_editable_and_presets_are_compact():
    source = INDEX_PATH.read_text(encoding="utf-8")
    print_row = _slice(
        source,
        '<div class="product-form-row product-print-row">',
        '<div class="product-form-row product-final-row">',
    )

    assert 'class="field product-printing-color-field"' in print_row
    assert ':title="productForm._printing_colors[index-1] || `第${index}色待填写`"' in print_row
    assert 'class="select product-printing-color-preset"' in print_row
    assert '<option value="黑色＋红色">黑＋红</option>' in print_row
    assert '<option value="黑色＋绿色">黑＋绿</option>' in print_row
    assert '<option value="黑色＋蓝色">黑＋蓝</option>' in print_row
    assert "product-printing-color-actions" not in print_row
    assert 'title="按图纸核对印刷内容，可能有印刷尺寸偏差"' in print_row


def test_large_mode_uses_the_same_compact_row_and_narrow_mode_may_wrap():
    source = INDEX_PATH.read_text(encoding="utf-8")
    modal = _slice(source, '<div ref="businessModal" class="modal"', '<div class="modal-head">')
    print_css_start = source.index(".product-print-row {")
    responsive_start = source.index("@media (max-width: 1100px)", print_css_start)
    desktop_css = source[print_css_start:responsive_start]
    narrow_css = source[responsive_start : source.index("@media (max-width: 680px)", responsive_start)]
    phone_css = source[source.index("@media (max-width: 680px)", responsive_start) : source.index("/* Toast */", responsive_start)]

    assert "'product-editor-large': modal.type === 'product' && isLargeUi" in modal
    assert ".product-editor-large .product-printing-direct-workbench" in desktop_css
    assert "flex-wrap:nowrap" in desktop_css
    assert ".product-printing-direct-workbench" in narrow_css
    assert "flex-wrap:wrap" in narrow_css
    assert ".product-editor-large .product-printing-direct-workbench" in phone_css
    assert "min-width:0" in phone_css
    assert "width:100%" in phone_css


def test_plate_rows_remain_full_width_and_payload_contract_is_untouched():
    source = INDEX_PATH.read_text(encoding="utf-8")
    plate_rule = _css_rule(source, ".product-printing-plate-config {")

    assert "flex:1 0 100%" in plate_rule
    assert "product-printing-plate-row" in source
    assert "product-printing-plate-parameters" in source
    assert "const printing = this.productPrintingWriteFields(this.productForm);" in source
    assert "Object.assign(payload,printing);" in source
    assert "delete payload._printing_colors" in source

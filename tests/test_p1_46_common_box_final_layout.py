from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PRODUCT_MODAL = INDEX.split(
    '<div v-else-if="modal.type === \'product\'">', 1
)[1].split(
    '<div v-else-if="modal.type === \'finishedStockPolicy\'">', 1
)[0]


def test_selected_layout_keeps_one_source_of_truth_for_visible_fields() -> None:
    bindings = {
        "长": 'v-model.number="productForm.length_mm"',
        "宽": 'v-model.number="productForm.width_mm"',
        "高": 'v-model.number="productForm.height_mm"',
        "拼箱方式": 'v-model="productForm.splice_mode"',
        "舌头": 'v-model.number="productForm.flap_mm"',
        "报料长": 'v-model.number="productForm.report_length_mm"',
        "报料宽": 'v-model.number="productForm.report_width_mm"',
        "压线类型": 'v-model="productForm.crease_type"',
        "压线左": 'v-model.number="productForm.crease_left_mm"',
        "压线中": 'v-model.number="productForm.crease_middle_mm"',
        "压线右": 'v-model.number="productForm.crease_right_mm"',
        "材质": 'v-model="productForm.material_id"',
        "层数": 'v-model.number="productForm.layer_count"',
        "楞型": 'v-model="productForm.flute_type"',
        "模具": 'v-model="productForm.mold_tool_id"',
        "印刷情况": 'v-model="productForm.print_content"',
        "印刷方式": 'v-model="productForm.printing_plate_mode"',
        "打印标签": 'v-model="productForm.production_label_enabled"',
        "标签数量": 'v-model="productForm.production_label_units_per_label"',
        "默认单价": 'v-model="productForm.sale_unit_price"',
    }
    for label, binding in bindings.items():
        assert binding in PRODUCT_MODAL, f"{label}没有继续绑定后端写入字段"

    # 支持自定义开料数的箱型仍使用已有规范化链路；A1 等固定箱型显示“一开一”。
    assert ':value="cuttingModeFactor(productForm.default_cutting_mode)"' in PRODUCT_MODAL
    assert 'productForm.default_cutting_mode=normalizeCuttingMode($event.target.value)' in PRODUCT_MODAL
    assert 'v-else class="inline-input"><span>一开一</span>' in PRODUCT_MODAL

    # 保存按钮必须复用原有 saveModal，不另造请求或局部保存逻辑。
    assert 'class="btn primary product-inline-save"' in PRODUCT_MODAL
    assert '@click="saveModal()"' in PRODUCT_MODAL
    assert '<div v-if="modal?.type !== \'product\'" class="modal-foot">' in INDEX


def test_layout_matches_selected_compact_information_architecture() -> None:
    size_row = PRODUCT_MODAL.split('class="product-form-row product-size-report-row"', 1)[1].split(
        'class="product-material-workbench"', 1
    )[0]
    regular_row = size_row.split("<template v-else>", 1)[1]
    assert regular_row.index("报料长宽") < regular_row.index("开料方式")
    assert regular_row.index("开料方式") < regular_row.index("压线类型")
    assert regular_row.index("压线类型") < regular_row.index("压线尺寸")

    final_row = PRODUCT_MODAL.split('class="product-form-row product-final-row"', 1)[1].split(
        "</fieldset>", 1
    )[0]
    assert final_row.index("默认单价") < final_row.index("图纸")
    assert final_row.index("图纸") < final_row.index("图纸记录")
    assert final_row.index("图纸记录") < final_row.index("product-inline-save")

    assert "productMaterialActiveTab==='candidates'" in PRODUCT_MODAL
    assert "productMaterialActiveTab==='history'" in PRODUCT_MODAL
    assert "组合产品 / 内部 BOM（不常用）" in PRODUCT_MODAL
    assert '<details class="product-secondary-disclosure">' in PRODUCT_MODAL


def test_digit_capacity_is_content_width_plus_control_chrome() -> None:
    css = INDEX.split("<style>", 1)[1].split("</style>", 1)[0]
    # 原来的 5ch/2ch 把 padding 和数字步进控件也算入总宽，导致一位数都看不见。
    assert "width: 5ch; min-width: 5ch" not in css
    assert "width: 2ch; min-width: 2ch" not in css
    assert ".product-dimension-field .input { width: 84px;" in css
    assert ".product-report-field .report-size-line .input { width: 104px;" in css
    assert ".product-production-label-quantity .input { width:68px;" in css
    assert 'max="999"' in PRODUCT_MODAL


def test_async_search_select_options_repaint_existing_backend_bindings() -> None:
    component = INDEX.split('app.component("search-select", {', 1)[1].split(
        'app.mount("#app")', 1
    )[0]
    assert "modelValue() { if (!this.open) this.query=this.selectedLabel; }," in component
    assert "options() { if (!this.open) this.query=this.selectedLabel; }," in component

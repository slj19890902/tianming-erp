from pathlib import Path


INDEX_HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_product_form_exposes_independent_default_cutting_mode() -> None:
    product_form = INDEX_HTML.split("modal.type === 'product'", 1)[1].split(
        "modal.type === 'customerScope'", 1
    )[0]
    core_row = product_form.split('class="product-form-row product-core-row"', 1)[1].split(
        'class="product-form-row product-size-report-row"', 1
    )[0]
    report_row = product_form.split('class="product-form-row product-size-report-row"', 1)[1]
    assert "<label>开料方式</label>" in report_row
    assert 'v-if="usesProductDefaultCuttingMode(productForm.box_style)"' in report_row
    assert 'v-model="productForm.default_cutting_mode"' in report_row
    assert "productForm.default_cutting_mode" not in core_row
    assert report_row.index('v-model="productForm.crease_type"') < report_row.index(
        'v-model="productForm.default_cutting_mode"'
    ) < report_row.index('class="field product-crease-field"')
    assert (
        'v-if="productSupportsCreaseSegments(productForm.box_style)" '
        'class="field product-crease-field"'
        in report_row
    )
    assert 'default_cutting_mode: "一开一"' in INDEX_HTML
    assert "default_cutting_mode: f.default_cutting_mode" in INDEX_HTML


def test_cutting_mode_visibility_and_flat_card_rename_follow_box_style() -> None:
    options = INDEX_HTML.split("productBoxStyleOptions: [", 1)[1].split("],", 1)[0]
    assert '"模切内盒"' in options
    assert '"平卡"' not in options
    assert '"隔板"' in options
    assert "?.supports_cutting_mode === true" in INDEX_HTML
    assert "productSupportedCuttingModes(productForm.box_style)" in INDEX_HTML
    assert "productSupportedCreaseTypes(productForm.box_style)" in INDEX_HTML
    special_select = INDEX_HTML.split(
        'class="field product-crease-type-field"', 1
    )[1].split("</select>", 1)[0]
    assert "productSupportedCreaseTypes(productForm.box_style)" in special_select
    assert "creaseTypeLabel(creaseType)" in special_select
    assert 'return value === "净料" ? "净" : value === "毛片" ? "毛" : value;' in INDEX_HTML


def test_requisition_form_keeps_manual_cutting_mode_override() -> None:
    requisition_table = INDEX_HTML.split('class="requisition-product-cell"', 1)[1]
    assert 'v-model="line.special_process" @change="autoRequisitionQty(line)"' in requisition_table
    for mode in ("一开一", "一开二", "一开三", "一开四", "一开五", "一开六"):
        assert f"<option>{mode}</option>" in requisition_table


def test_active_supplier_draft_recalculates_manual_cutting_mode_override() -> None:
    supplier_draft = INDEX_HTML.split("modal.type === 'supplierRequisitionDraft'", 1)[1].split(
        "modal.type === 'requisition'", 1
    )[0]
    assert '@change="recalculateSupplierDraftLine(line)"' in supplier_draft
    method = INDEX_HTML.split("recalculateSupplierDraftLine(line) {", 1)[1].split(
        "mergeGroupPayload(row)", 1
    )[0]
    assert "line.remaining_required_piece_qty" in method
    assert "this.cuttingModeFactor(line.cutting_mode)" in method
    assert "Math.ceil(" in method

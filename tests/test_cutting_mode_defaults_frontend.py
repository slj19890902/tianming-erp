from pathlib import Path


INDEX_HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_product_form_exposes_independent_default_cutting_mode() -> None:
    assert "默认开料方式（每张报料纸产出）" in INDEX_HTML
    assert 'v-model="productForm.default_cutting_mode"' in INDEX_HTML
    assert 'default_cutting_mode: "一开一"' in INDEX_HTML
    assert "default_cutting_mode: f.default_cutting_mode" in INDEX_HTML
    assert "productForm.default_cutting_mode" not in INDEX_HTML.split(
        '<label>每箱片数</label>', 1
    )[0][-200:]


def test_requisition_form_keeps_manual_cutting_mode_override() -> None:
    requisition_table = INDEX_HTML.split('class="requisition-product-cell"', 1)[1]
    assert 'v-model="line.special_process" @change="autoRequisitionQty(line)"' in requisition_table
    for mode in ("一开一", "一开二", "一开三", "一开四", "一开五"):
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

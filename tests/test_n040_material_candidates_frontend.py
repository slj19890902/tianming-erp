import re
from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_daily_product_toolbar_has_no_global_material_candidate_entry() -> None:
    source = _source()
    products_page_start = source.index('<template v-else-if="activePage === \'products\'">')
    products_table_start = source.index('<div v-if="productTab === \'products\'">', products_page_start)
    daily_toolbar = source[products_page_start:products_table_start]
    assert "客户材质候选" not in daily_toolbar
    assert "openMaterialCandidateMaintenance" not in daily_toolbar


def test_common_box_editor_embeds_product_specific_candidate_and_requisition_history() -> None:
    source = _source()
    editor_start = source.index('<div class="product-material-workbench">')
    editor_context = source[editor_start:editor_start + 8000]
    candidate_heading = "这款常用箱的候选材质"
    history_heading = "这款常用箱历史报料材质"
    assert candidate_heading in editor_context
    assert history_heading in editor_context
    assert editor_context.index(candidate_heading) < editor_context.index(history_heading)
    assert "candidate.supplier_name" in editor_context
    assert "history.supplier_name" in editor_context
    assert 'axios.get(`/api/requisition/products/${requestedId}/material-context`)' in source
    assert "applyProductMaterialCandidate(candidate)" in source
    assert "formatBeijingDate(candidate.last_used_at)" in source
    assert "dt(candidate.last_used_at)" not in source
    assert "只回填当前表单，仍需保存常用箱" in source
    assert "不混入同客户其他产品" in source
    assert 'this.hasPermission("requisition.view")' in source
    assert "当前账号无报料历史查看权限" in source


def test_common_box_candidate_column_is_narrower_than_history_column() -> None:
    source = _source()
    grid_rule_start = source.index(".product-material-workbench {")
    grid_rule = source[grid_rule_start:source.index("}", grid_rule_start)]
    column_fractions = [float(value) for value in re.findall(r"minmax\([^,]+,\s*([0-9.]+)fr\)", grid_rule)]
    assert len(column_fractions) == 3
    assert column_fractions[1] < column_fractions[2]


def test_common_box_candidate_keeps_flute_and_history_does_not_mutate_form() -> None:
    source = _source()
    assert "const currentFlute = this.productForm.flute_type;" in source
    assert "this.productForm.flute_type = currentFlute;" in source
    assert '@click="applyProductMaterialCandidate(candidate)"' in source
    history_loop = 'v-for="history in productMaterialContext.requisition_history"'
    assert history_loop in source
    history_section = source[source.index(history_loop):source.index(history_loop) + 1600]
    assert "productForm.material_id" not in history_section


def test_common_box_material_context_stacks_on_narrow_screens() -> None:
    source = _source()
    narrow_start = source.index("@media (max-width: 680px)")
    narrow_rules = source[narrow_start:narrow_start + 1200]
    assert ".product-material-workbench," in narrow_rules
    assert ".product-material-controls { grid-template-columns: 1fr; }" in narrow_rules


def test_pending_material_candidate_keeps_manual_confirmation_and_string_reference() -> None:
    source = _source()
    assert 'axios.get(`/api/requisition/pending/${itemId}/material-candidates`)' in source
    assert 'axios.get(`/api/requisition/pending/${itemId}/material-history`)' in source
    assert "本明细选择历史" in source
    assert "只追加，不覆盖历史事实" in source
    assert 'form.candidate_id = this.requisitionCandidateId(candidate);' in source
    assert (
        'form.source_reference = String(candidate.source_reference || '
        'candidate.reference_id || form.candidate_id || "") || null;'
    ) in source
    assert 'sync_product: false' in source
    assert "同步回订单关联的常用箱" in source
    assert '<span v-if="canViewCosts">参考价：' in source

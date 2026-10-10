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
    editor_context = source[editor_start:editor_start + 20000]
    candidate_heading = "这款常用箱的候选材质"
    history_heading = "这款常用箱的报料材质轨迹"
    assert candidate_heading in editor_context
    assert history_heading in editor_context
    assert editor_context.index(candidate_heading) < editor_context.index(history_heading)
    assert "candidate.supplier_name" in editor_context
    assert "history.supplier_name" in editor_context
    assert 'axios.get(`/api/requisition/products/${requestedId}/material-context`, {' in source
    assert "signal:controller.signal" in source
    assert "applyProductMaterialCandidate(candidate)" in source
    assert "formatDateTime(candidate.last_requisition_at)" in source
    assert "dt(candidate.last_used_at)" not in source
    assert "只回填当前表单，仍需保存常用箱" in source
    assert "尚未匹配正式报料单的人工选材单独标识" in source
    assert "不混入同客户其他产品" in source
    assert 'this.hasPermission("requisition.view")' in source
    assert "当前账号无报料历史查看权限" in source
    assert "candidate.basis_weight_description" in editor_context
    assert "candidate.total_basis_weight_gsm" in editor_context
    assert "candidate.effective_price" in editor_context
    assert "最近报料" in editor_context
    assert "报料次数" in editor_context
    assert "product-material-candidate-summary" in editor_context
    assert "product-material-candidate-identity" in editor_context
    assert "toggleProductMaterialCandidateComparison(candidate)" in editor_context
    assert "候选材质对比" in editor_context
    assert "一次最多对比 4 款候选材质" in source
    candidate_section = editor_context[editor_context.index(candidate_heading):editor_context.index(history_heading)]
    assert "candidate.last_requisition_at" not in candidate_section
    assert "candidate.history_count" not in candidate_section
    assert "candidate.effective_price" not in candidate_section
    assert "candidate.basis_weight_description" not in candidate_section
    assert "candidate.total_basis_weight_gsm" not in candidate_section
    assert "product-material-candidate-metrics" not in candidate_section
    assert "toggleProductMaterialCandidateDetail" not in candidate_section
    assert candidate_section.index("candidate.material_code") < candidate_section.index("toggleProductMaterialCandidateComparison(candidate)")
    assert candidate_section.index("toggleProductMaterialCandidateComparison(candidate)") < candidate_section.index("applyProductMaterialCandidate(candidate)")
    assert "报料选材" not in candidate_section
    assert "candidate.recommendation_reason" not in candidate_section
    assert "candidate.last_document_no" not in candidate_section
    assert "history.basis_weight_description" in editor_context
    assert "history.total_basis_weight_gsm" in editor_context
    assert "history.current_effective_price" in editor_context
    assert "当前材质档案价，非报料时冻结价" in editor_context
    assert "toggleProductMaterialHistoryDetail(history)" in editor_context


def test_common_box_comparison_aligns_colored_fields_for_each_material() -> None:
    source = _source()
    comparison_start = source.index('<section v-if="productMaterialComparedCandidates.length"')
    comparison_end = source.index('</section>', comparison_start)
    comparison = source[comparison_start:comparison_end]
    assert "product-material-comparison-table" in comparison
    assert "'--compare-count': productMaterialComparedCandidates.length" in comparison
    for row_class in (
        "product-material-comparison-row--material",
        "product-material-comparison-row--time",
        "product-material-comparison-row--count",
        "product-material-comparison-row--price",
        "product-material-comparison-row--weight",
        "product-material-comparison-row--structure",
        "product-material-comparison-row--paper",
        "product-material-comparison-row--quote",
    ):
        assert row_class in comparison
        assert f".{row_class} > *" in source
    for label in ("最近报料时间", "报料次数", "当前单价", "总克重", "克重结构", "纸种结构", "报价日期"):
        assert label in comparison
    for tone in range(4):
        assert f".product-material-comparison-value.candidate-tone-{tone}" in source
    assert "isProductMaterialComparedLowestPrice(candidate)" in comparison
    assert "productMaterialComparedLowestPrice()" in source
    assert "所选最低" in comparison
    assert "product-material-comparison-card" not in comparison


def test_common_box_requisition_trajectory_keeps_summary_short_and_details_explicit() -> None:
    source = _source()
    history_loop = 'v-for="history in productMaterialContext.material_history"'
    history_start = source.index(history_loop)
    history_end = source.index("</section>", history_start)
    history = source[history_start:history_end]
    summary_start = history.index('<div class="product-material-history-summary">')
    detail_start = history.index('<div v-if="isProductMaterialHistoryDetailOpen(history)"')
    summary = history[summary_start:detail_start]
    details = history[detail_start:]
    assert "formatProductMaterialHistoryTime(history)" in summary
    assert "history.material_code" in summary
    assert "history.supplier_name" in summary
    assert "history.requisition_qty" in summary
    assert "history.document_no" not in summary
    assert "history.basis_weight_description" not in summary
    assert "history.total_basis_weight_gsm" not in summary
    assert "history.layer_count" not in summary
    assert "history.flute_type" not in summary
    assert "报料单号" in details and "history.document_no" in details
    assert "克重" in details and "history.basis_weight_description" in details
    assert "history.total_basis_weight_gsm" in details
    assert "层数 / 楞型" in details
    assert "history.current_effective_price" in details


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
    history_loop = 'v-for="history in productMaterialContext.material_history"'
    assert history_loop in source
    history_section = source[source.index(history_loop):source.index(history_loop) + 1600]
    assert "productForm.material_id" not in history_section
    assert "history.is_formal_requisition === false" in history_section
    history_key_method = source[source.index("productMaterialHistoryKey(history) {"):source.index("isProductMaterialHistoryDetailOpen(history) {")]
    assert "history?.document_item_id" in history_key_method


def test_common_box_material_context_stacks_on_narrow_screens() -> None:
    source = _source()
    narrow_start = source.index("@media (max-width: 680px)")
    narrow_rules = source[narrow_start:narrow_start + 1800]
    assert ".product-material-workbench," in narrow_rules
    assert ".product-material-controls { grid-template-columns: 1fr; }" in narrow_rules
    assert "repeat(var(--compare-count), minmax(0, 1fr))" in narrow_rules
    assert "overflow-x" not in narrow_rules


def test_common_box_candidate_identity_and_actions_share_one_compact_row() -> None:
    source = _source()
    rule_start = source.index(".product-material-candidate-summary {")
    rule = source[rule_start:source.index("}", rule_start)]
    assert "display: flex" in rule
    assert "justify-content: space-between" in rule
    candidate_start = source.index('<div v-for="candidate in productMaterialContext.candidates"')
    candidate_end = source.index("</section>", candidate_start)
    candidate = source[candidate_start:candidate_end]
    assert candidate.index("product-material-candidate-identity") < candidate.index("product-material-candidate-actions")
    assert "product-material-detail-button" not in candidate


def test_pending_material_candidate_keeps_confirmation_and_auto_sync_reference() -> None:
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
    assert "sync_product: !!row.product_id" in source
    assert "保存后自动同步订单关联的常用箱" in source
    assert "保存报料材质并同步常用箱" in source
    assert '<span v-if="canViewCosts">参考价：' in source

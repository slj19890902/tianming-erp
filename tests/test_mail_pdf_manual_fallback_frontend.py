from pathlib import Path


INDEX = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def test_failed_pdf_exposes_explicit_manual_entry_flow() -> None:
    assert "按原 PDF 人工录入" in INDEX
    assert "新增人工明细" in INDEX
    assert "核对并匹配常用箱" in INDEX
    assert '/api/orders/draft-manual-rematch' in INDEX
    assert "manual_complete:true" in INDEX


def test_manual_entry_still_requires_signed_integrity_and_product_matching() -> None:
    reason_start = INDEX.index("importDraftBlockReasons(draft) {")
    reason_end = INDEX.index("toggleConfirmableImportDrafts()", reason_start)
    reasons = INDEX[reason_start:reason_end]
    assert '["passed","manual_confirmed"]' in reasons
    assert "!item.matched_product_id" in reasons
    assert "数量必须大于0" in reasons
    assert "缺少客户单价" in reasons


def test_manual_customer_change_waits_for_operator_before_rematch() -> None:
    assert '@change="handleImportDraftCustomerChange(draftIndex)"' in INDEX
    start = INDEX.index("async handleImportDraftCustomerChange(index) {")
    end = INDEX.index("async confirmManualPdfDraft(index) {", start)
    handler = INDEX[start:end]
    assert "if (draft.manual_entry)" in handler
    assert "await this.rematchImportDraft(index)" in handler

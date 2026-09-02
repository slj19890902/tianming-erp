from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    assert start_marker in INDEX, f"missing frontend contract marker: {start_marker}"
    start = INDEX.index(start_marker)
    assert end_marker in INDEX[start:], f"missing frontend contract marker after {start_marker}: {end_marker}"
    return INDEX[start : INDEX.index(end_marker, start)]


def test_stock_warning_composite_parent_starts_with_editable_set_plan() -> None:
    """The warning flow must show parent sets before any component purchase rows."""

    for label in ("当前完整套", "预警套", "目标套", "建议补套", "确认补库套数"):
        assert label in INDEX
    assert "stockReplenishmentForm.composite_parent_plan" in INDEX
    assert 'v-model.number="stockReplenishmentForm.composite_parent_plan.parent_set_quantity"' in INDEX
    assert '@click="confirmStockCompositeParentPlan"' in INDEX
    assert "确认后展开组件" in INDEX
    assert "policy.is_virtual_composite_parent ? '套' : '张'" in INDEX


def test_stock_warning_parent_confirmation_is_zero_write_until_expand() -> None:
    open_block = _block("async addStockPolicyDraft(policy)", "async addCompatibleStockDraft")
    assert "data.composite_parent_plan" in open_block
    assert "this.stockReplenishmentForm.composite_parent_plan" in open_block
    assert "this.stockReplenishmentForm.items = []" in open_block

    confirm_block = _block(
        "async confirmStockCompositeParentPlan",
        "async addCompatibleStockDraft",
    )
    assert "parent_set_quantity:" in confirm_block
    assert "axios." in confirm_block
    assert "data.items" in confirm_block
    assert "this.stockReplenishmentForm.items" in confirm_block
    assert "beginLatestRequest" in confirm_block
    assert "controller.signal" in confirm_block
    assert "latestRequestControllers.get(requestKey) !== controller" in confirm_block
    assert "stockCompositeParentConfirming" in confirm_block
    assert "isCancelledRequest" in confirm_block
    open_block = _block("async openStockReplenishment(options={})", "addBlankStockReplenishmentLine()")
    assert 'cancelLatestRequest("requisition:stock-composite-parent-confirm")' in open_block
    close_block = _block("closeModal() {", "handleMasterSaveRefreshFailure")
    assert 'this.modal?.type === "stockReplenishment"' in close_block
    assert 'cancelLatestRequest("requisition:stock-composite-parent-confirm")' in close_block
    product_block = _block("async applyStockProduct(line, options={})", "applyStockMaterial(line)")
    assert product_block.index("cancelLatestRequest") < product_block.index("const product =")
    assert "confirm(" not in open_block
    assert "auto-cover" not in open_block


def test_expanded_components_show_sets_pieces_sheets_and_manual_green_deduction() -> None:
    for label in ("父件套数", "组件需求", "可抵扣", "净需求", "一张出", "采购张数"):
        assert label in INDEX
    assert "备用张数" in INDEX
    assert "stockCompositeComponentFormula(line)" in INDEX
    formula = _block("stockCompositeComponentFormula(line) {", "stockWarningTheoreticalSheets(line) {")
    for unit in ("套", "片", "张"):
        assert unit in formula
    for field in (
        "parent_set_quantity",
        "bom_quantity_per_set",
        "required_piece_quantity",
        "inventory_deducted_piece_quantity",
        "net_required_piece_quantity",
        "yield_per_sheet",
        "spare_sheet_quantity",
        "purchase_sheet_quantity",
    ):
        assert field in formula

    assert 'class="btn small success"' in INDEX
    assert '@click="confirmCompositeParentInventoryDeduction(line)"' in INDEX
    assert ">抵扣</button>" in INDEX
    deduction = _block(
        "async confirmCompositeParentInventoryDeduction(line)",
        "stockCompositeComponentFormula(line)",
    )
    assert "inventory" in deduction.lower()
    assert "await" in deduction
    assert ".sort(" not in deduction
    assert ".parent_set_quantity =" not in deduction
    assert "netPurchaseSheets" in deduction
    assert "spare_sheet_quantity" in deduction
    assert "netPurchaseSheets + spareSheets" in deduction

    validation = _block(
        "validateStockReplenishmentForm() {",
        "async saveStockPolicyFromLine(line)",
    )
    assert "netPurchaseSheets" in validation
    assert "spare_sheet_quantity" in validation
    assert "netPurchaseSheets + spareSheets" in validation


def test_stock_replenishment_payload_keeps_parent_plan_but_never_buys_virtual_parent() -> None:
    save_block = _block("async saveStockReplenishmentDraft()", "async saveSupplierRequisitionDraft()")
    assert "composite_parent_plan:" in save_block
    assert "this.stockReplenishmentForm.composite_parent_plan" in save_block
    assert ".filter(line => !line.is_virtual_composite_parent)" in save_block
    assert "items:this.stockReplenishmentForm.items" in save_block
    assert "parent_set_quantity" in save_block


def test_order_composite_requisition_confirms_read_only_parent_sets_before_component_draft() -> None:
    open_block = _block("async openCompositeRequisition(rows)", "confirmCompositeOrderParentSets()")
    assert "compositeParentConfirmation" in open_block
    assert "parent_set_quantity" in open_block
    assert "items:[]" in open_block
    assert "buildRequisitionFormLines" not in open_block
    assert "报料父件套数确认" in open_block

    confirm_block = _block(
        "confirmCompositeOrderParentSets()",
        "recalculateCompositeDraftLine(line)",
    )
    assert "buildRequisitionFormLines" in confirm_block
    assert "parent_set_quantity" in confirm_block
    assert "this.requisitionForm" in confirm_block
    assert "items:lines" in confirm_block
    assert 'this.modal = {type:"requisition", title:"报料明细草稿"}' in confirm_block
    assert "axios.post(\"/api/requisition/batches\"" not in open_block


def test_order_composite_requisition_preserves_frozen_spare_sheets() -> None:
    recalculate_block = _block(
        "recalculateCompositeDraftLine(line) {",
        "async autoCoverCompositeDraftLine(line)",
    )
    assert "source.spare_sheet_quantity" in recalculate_block
    assert "line.spare_sheet_quantity" in recalculate_block
    assert "Math.ceil(remaining / Math.max(yieldPerSheet,1)) + spareSheets" in recalculate_block
    assert "Math.ceil(remaining / Math.max(yieldPerSheet, 1)) + spareSheets" in recalculate_block

    grouped_payload_block = _block(
        "requisitionBatchLinePayloads(line) {",
        "async openRequisition()",
    )
    assert "source.spare_sheet_quantity" in grouped_payload_block
    assert "Math.ceil(remaining / Math.max(Number(source.actual_yield_per_sheet || 0) || factor,1)) + spareSheets" in grouped_payload_block

    build_lines_block = _block(
        "buildRequisitionFormLines(row) {",
        "normalizeReceiptResolution(line)",
    )
    assert "spare_sheet_quantity:Number(component.spare_sheet_quantity || 0)" in build_lines_block


def test_zero_purchase_component_plan_is_visible_but_not_supplier_printable() -> None:
    reported_block = _block(
        "reportedSelectedItems() {",
        "async openReportedItemDocument(row)",
    )
    assert ".filter(row => !row.is_plan_only)" in reported_block
    assert "row?.is_plan_only" in reported_block
    assert "库存已抵扣，无需采购" in reported_block

    document_block = _block(
        "async openReportedItemDocument(row)",
        "reportedItemVoidAttempt(row)",
    )
    assert "if (row?.is_plan_only)" in document_block
    assert "本次没有供应商采购单，无需打印" in document_block


def test_zero_purchase_component_plan_save_skips_empty_purchase_print() -> None:
    save_block = _block(
        "async saveStockReplenishmentDraft()",
        "async saveSupplierRequisitionDraft()",
    )
    no_purchase_guard = (
        'Array.isArray(data.items) && data.items.length === 0 '
        '&& Array.isArray(data.component_plans) && data.component_plans.length > 0'
    )
    assert no_purchase_guard in save_block
    guard_index = save_block.index(no_purchase_guard)
    print_index = save_block.index(
        "axios.get(`/api/requisition/stock-replenishment/orders/${data.id}/print`)"
    )
    assert guard_index < print_index
    assert "组合组件库存已预占，本次无需供应商报料" in save_block

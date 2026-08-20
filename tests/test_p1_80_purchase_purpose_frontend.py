from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_source(start: str, end: str) -> str:
    start_index = INDEX_HTML.index(start)
    end_index = INDEX_HTML.index(end, start_index)
    return INDEX_HTML[start_index:end_index]


def _run_purpose_helper(script: str) -> dict:
    helper = _method_source(
        "purchasePurposeRawValue(line, key) {",
        "supplierOrderPurposeValue(line, key, fallback = null) {",
    )
    completed = subprocess.run(
        [
            "node",
            "-e",
            f"const helpers=({{{helper}}});\n{script}",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def test_supplier_draft_purchase_total_defaults_excess_to_customer_stock() -> None:
    result = _run_purpose_helper(
        """
const line={remaining_requisition_qty:500,requisition_qty:500,
  purchase_total_sheet_qty:500,order_purpose_sheet_qty:500,
  stock_purpose_sheet_qty:0,purpose_plan_fingerprint:'a'.repeat(64),
  purpose_plan_version:1};
helpers.initializePurchasePurposeLine.call(helpers,line);
line.purchase_total_sheet_qty=600;
helpers.onPurchasePurposeTotalChanged.call(helpers,line);
console.log(JSON.stringify(line));
"""
    )
    assert result["purchase_total_sheet_qty"] == 600
    assert result["requisition_qty"] == 600
    assert result["order_purpose_sheet_qty"] == 500
    assert result["stock_purpose_sheet_qty"] == 100
    assert result["purpose_plan_fingerprint"] == "a" * 64
    assert result["purpose_plan_version"] == 1


def test_supplier_draft_renders_units_and_sends_complete_purpose_contract() -> None:
    modal = INDEX_HTML.split("modal.type === 'supplierRequisitionDraft'", 1)[1].split(
        "modal.type === 'requisition'", 1
    )[0]
    save = _method_source(
        "async saveSupplierRequisitionDraft() {",
        "supplierRequisitionSelectionSignature(selections) {",
    )
    for text in (
        "订单生产用途",
        "客户通用片料备库",
        "采购总张",
        "张",
    ):
        assert text in modal
    for field in (
        "purchase_total_sheet_qty",
        "order_purpose_sheet_qty",
        "stock_purpose_sheet_qty",
        "purpose_plan_version",
        "purpose_plan_fingerprint",
    ):
        assert field in save
    assert "只" in modal


def test_formal_supplier_order_purpose_is_read_only_and_not_editable() -> None:
    formal = INDEX_HTML.split('aria-label="厂内采购用途只读详情"', 1)[1].split(
        '<div v-if="modal.data.focus_reported_item', 1
    )[0]
    assert "订单生产用途" in formal
    assert "客户通用片料备库" in formal
    assert "purpose_status" in formal
    assert 'v-model.number="line.order_purpose_sheet_qty"' not in formal
    assert 'v-model.number="line.stock_purpose_sheet_qty"' not in formal


def test_supplier_print_projection_never_exposes_internal_purpose_split() -> None:
    print_area = INDEX_HTML.split('id="supplier-order-print-area"', 1)[1].split(
        'aria-label="厂内采购用途只读详情"', 1
    )[0]
    assert "requisition_qty" in print_area
    assert "order_purpose_sheet_qty" not in print_area
    assert "stock_purpose_sheet_qty" not in print_area
    assert "客户通用片料备库" not in print_area


def test_normal_confirmation_has_no_new_reason_or_second_confirmation() -> None:
    save = _method_source(
        "async saveSupplierRequisitionDraft() {",
        "supplierRequisitionSelectionSignature(selections) {",
    )
    assert "purpose_reason" not in save
    assert "window.confirm" not in save
    assert "confirm(" not in save


def test_composite_frozen_payload_keeps_request_key_across_uncertain_reopen() -> None:
    save = _method_source(
        "async saveCompositeRequisitionDraft() {",
        "async saveStockReplenishmentDraft() {",
    )
    reopen = _method_source(
        "async openCompositeRequisition(rows) {",
        "recalculateCompositeDraftLine(line) {",
    )

    assert 'if (!String(state.requestKey || "").trim())' in save
    assert "state.requestKey = createIdempotencyKey()" in save
    assert "request_key:state.requestKey" in save
    assert "const frozenPayload = JSON.parse(JSON.stringify(payload))" in save
    assert 'axios.post("/api/requisition/batches", frozenPayload)' in save
    assert "state.uncertain = true" in save

    assert "const previousSaveState = this.compositeRequisitionSaveState || {}" in reopen
    assert "previousSaveState.uncertain" in reopen
    assert "previousSaveState.draftSignature === draftSignature" in reopen
    assert 'String(previousSaveState.requestKey || "").trim()' in reopen
    assert "? previousSaveState.requestKey" in reopen
    assert ": createIdempotencyKey()" in reopen
    assert "requestKey, draftSignature" in reopen


def test_composite_rows_never_fall_through_to_ordinary_supplier_preview() -> None:
    routing = _method_source(
        "async openSupplierRequisitionDraft(rows = null) {",
        "async openCompositeRequisition(rows) {",
    )
    composite_save = _method_source(
        "async saveCompositeRequisitionDraft() {",
        "async saveStockReplenishmentDraft() {",
    )

    composite_gate = routing.split("const selections =", 1)[0]
    assert "const compositeRows = selectedRows.filter(row => row.is_composite_bom)" in composite_gate
    assert "if (compositeRows.length !== selectedRows.length)" in composite_gate
    assert "return this.openCompositeRequisition(compositeRows)" in composite_gate
    assert "/api/requisition/supplier-orders/preview-from-pending-selection" in routing
    assert 'axios.post("/api/requisition/batches", frozenPayload)' in composite_save

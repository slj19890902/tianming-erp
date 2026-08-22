from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def _source(document: str, start: str, end: str) -> str:
    start_index = document.index(start)
    end_index = document.index(end, start_index)
    return document[start_index:end_index]


def test_desktop_frozen_rows_render_server_preview_and_hide_legacy_decisions() -> None:
    table = _source(
        DESKTOP,
        '<table class="incoming-table">',
        "<pager v-if=\"incomingTab==='pending'\"",
    )
    for field in (
        "purpose_status",
        "预计订单用途",
        "预计片料备库",
        "预计形成成品",
        "finished_location_name",
        "reserve_location_name",
    ):
        assert field in table

    # Manual short/over decisions remain a compatibility path only for a
    # historical line that explicitly says it has no frozen purpose fact.
    assert "row.purpose_status==='legacy_unset'" in table
    legacy_block = table.split("row.purpose_status==='legacy_unset'", 1)[1]
    assert "少收怎么处理" in legacy_block
    assert "多收怎么处理" in legacy_block
    frozen_prefix = table.split("row.purpose_status==='legacy_unset'", 1)[0]
    assert "少收怎么处理" not in frozen_prefix
    assert "多收怎么处理" not in frozen_prefix


def test_desktop_receipt_result_hides_material_and_purchase_price_status_hint() -> None:
    table = _source(
        DESKTOP,
        '<table class="incoming-table">',
        "<pager v-if=\"incomingTab==='pending'\"",
    )
    assert "请先确认实际材质和正式采购价格" not in table
    assert "待确认实际材质和正式采购价" not in table
    assert "预计订单用途" in table
    assert "容量提醒" in table


def test_desktop_receive_payload_carries_versions_not_client_allocations_or_price() -> None:
    payload = _source(
        DESKTOP,
        "incomingPayload(row) {",
        "incomingSelectedCount() {",
    )
    for field in (
        "expected_receipt_fact_version",
        "purchase_purpose_source_snapshot_id",
        "expected_purpose_snapshot_version",
        "receipt_plan_fingerprint",
        "expected_actual_material_version",
        "actual_material_fingerprint",
    ):
        assert field in payload
    for forbidden in (
        "order_sheet_delta",
        "reserve_sheet_delta",
        "order_purpose_sheet_qty",
        "stock_purpose_sheet_qty",
        "unit_price",
        "quote_price",
        "finished_location_id",
        "reserve_location_id",
    ):
        assert forbidden not in payload

    batch = _source(
        DESKTOP,
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    assert "this.incomingPayload(row)" in batch
    assert "/api/incoming/batch-receive" in batch


def test_desktop_failed_or_uncertain_receive_keeps_input_and_idempotency_key() -> None:
    receive = _source(
        DESKTOP,
        "async receiveIncoming(row) {",
        "async acceptShortIncoming(row) {",
    )
    assert "previousAttempt?.idempotencyKey" in receive
    assert "attempt.payload" in receive
    assert "attempt.committed" in receive
    # A failure may be reconciled or retried, but must not discard the stable
    # key merely because the HTTP status is below 500.
    assert "error?.response?.status < 500" not in receive
    assert "delete this.incomingReceiveAttempts[attemptKey]" not in receive


def test_desktop_normal_batch_receive_has_no_confirmation_dialog() -> None:
    batch = _source(
        DESKTOP,
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    assert "确认将选中的" not in batch
    assert 'axios.put("/api/incoming/batch-receive"' in batch


def test_purchase_receipt_fact_editor_uses_formal_contract_and_never_quote_price() -> None:
    editor = _source(
        DESKTOP,
        "async openPurchaseReceiptFact(row) {",
        "incomingPayload(row) {",
    )
    assert "/api/requisition/purchase-sources/" in editor
    assert "/receipt-facts" in editor
    for field in (
        "actual_material_id",
        "unit_price",
        "currency",
        "price_unit",
        "tax_included",
        "tax_rate",
        "purchase_purpose_source_snapshot_id",
        "purpose_snapshot_version",
        "receipt_plan_fingerprint",
        "expected_source_version",
        "expected_latest_receipt_fact_version",
        "material_variance_approval_id",
        "idempotency_key",
    ):
        assert field in editor
    assert "material_change_confirmed" not in editor

    receipt_fact_call = DESKTOP.split(
        "/api/requisition/purchase-sources/", 1
    )[1].split("/receipt-facts", 1)[0]
    assert "quote_price" not in receipt_fact_call
    assert "materials.quote_price" not in DESKTOP


def test_desktop_material_variance_is_requested_and_confirmed_independently() -> None:
    editor = _source(
        DESKTOP,
        "async openPurchaseReceiptFact(row) {",
        "incomingPayload(row) {",
    )
    assert "/material-variances" in editor
    assert "/api/requisition/purchase-material-variances/" in editor
    assert "material_variance_approval_id" in editor


def test_desktop_normal_receipt_uses_material_cell_and_auto_freezes_master_price() -> None:
    table = _source(
        DESKTOP,
        '<table class="incoming-table">',
        "<pager v-if=\"incomingTab==='pending'\"",
    )
    assert "openPurchaseReceiptMaterialChange(row)" in table
    assert "处理材质与采购价" not in table

    material_editor = _source(
        DESKTOP,
        "async openPurchaseReceiptMaterialChange(row) {",
        "async savePurchaseReceiptFact() {",
    )
    for field in (
        "material_search",
        "purchaseReceiptMaterialCandidates()",
        "selectPurchaseReceiptMaterial(material)",
        "/material-variances",
    ):
        assert field in material_editor
    for forbidden in ("unit_price", "currency", "price_unit", "tax_rate"):
        assert forbidden not in material_editor

    automatic = _source(
        DESKTOP,
        "async ensureAutomaticPurchaseReceiptFact(row) {",
        "incomingPayload(row) {",
    )
    assert "/receipt-facts/auto" in automatic
    assert "material_variance_approval_id" in automatic
    assert "unit_price" not in automatic
    assert 'currency: "CNY"' not in automatic
    assert "tax_rate: 0.13" not in automatic
    assert "row.purpose_status==='frozen'" in _source(
        DESKTOP,
        "canReceiveIncoming(row) {",
        "incomingSelectedCount() {",
    )


def test_mobile_uses_same_server_facts_and_does_not_recalculate_purpose() -> None:
    receive = _source(
        MOBILE,
        "async function receive(itemId) {",
        "async function acceptShortNow(itemId) {",
    )
    for field in (
        "expected_receipt_fact_version",
        "purchase_purpose_source_snapshot_id",
        "expected_purpose_snapshot_version",
        "receipt_plan_fingerprint",
        "expected_actual_material_version",
        "actual_material_fingerprint",
    ):
        assert field in receive
    assert "/api/incoming/receive/" in receive
    payload = _source(receive, "body: JSON.stringify({", "}),")
    for forbidden in (
        "order_purpose_sheet_qty",
        "stock_purpose_sheet_qty",
        "order_sheet_delta",
        "reserve_sheet_delta",
        "unit_price",
    ):
        assert forbidden not in payload

    render = _source(MOBILE, "function renderCard(item) {", "function applyIncomingPrimarySpecLayout")
    assert "purpose_status" in render
    assert "legacy_unset" in render
    assert "预计订单用途" in render
    assert "预计片料备库" in render
    assert "预计形成成品" in render
    assert "finished_location_name" in render
    assert "reserve_location_name" in render

    automatic = _source(
        MOBILE,
        "async function ensureAutomaticReceiptFact(item) {",
        "async function receive(itemId) {",
    )
    assert "/receipt-facts/auto" in automatic
    assert "unit_price" not in automatic
    assert "await ensureAutomaticReceiptFact(item);" in receive


def test_mobile_success_and_history_render_formal_purpose_facts() -> None:
    receive = _source(
        MOBILE,
        "async function receive(itemId) {",
        "async function acceptShortNow(itemId) {",
    )
    assert "result.purpose_allocation" in receive

    render = _source(
        MOBILE,
        "function renderCard(item) {",
        "function applyIncomingPrimarySpecLayout",
    )
    for field in (
        "item.purpose_allocation",
        "item.purpose_reversal",
        "order_sheet_delta",
        "reserve_sheet_delta",
    ):
        assert field in render


def test_desktop_and_mobile_revert_keep_stable_idempotency_keys() -> None:
    desktop_revert = _source(
        DESKTOP,
        "async revertIncoming(row) {",
        "async openIncomingProductionCard(row) {",
    )
    assert "row._revert_idempotency_key=row._revert_idempotency_key ||" in desktop_revert
    assert "{idempotency_key:row._revert_idempotency_key}" in desktop_revert

    mobile_revert = _source(
        MOBILE,
        '$("revertForm").addEventListener("submit"',
        "async function retryCurrentView() {",
    )
    assert "state.revertIdempotencyKeys.get(key) ||" in mobile_revert
    assert "state.revertIdempotencyKeys.set(key, revertKey)" in mobile_revert
    assert "JSON.stringify({idempotency_key: revertKey})" in mobile_revert


@pytest.mark.parametrize(
    ("name", "document"),
    (("desktop-index", DESKTOP), ("mobile-incoming", MOBILE)),
    ids=("desktop", "mobile"),
)
def test_inline_javascript_passes_node_check(
    tmp_path: Path,
    name: str,
    document: str,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P1-81 frontend contract"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            document,
            flags=re.DOTALL,
        )
        if script.strip()
    ]
    assert scripts
    target = tmp_path / f"{name}.js"
    target.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

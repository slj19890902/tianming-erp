from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    start_at = INDEX.index(start)
    return INDEX[start_at : INDEX.index(end, start_at)]


def test_requisition_has_three_clear_tabs_and_batch_hold_entry() -> None:
    page = _block("<template v-else-if=\"activePage === 'requisition'\">", "<template v-else-if=\"activePage === 'incoming'\">")

    assert "待报料 {{ requisitionPendingOverallTotal }}" in page
    assert "等候报料 {{ requisitionHoldSummary.total }}" in page
    assert "（到期 {{ requisitionHoldSummary.due_count }}）" in page
    assert "已报料/已入库" in page
    assert '@click="openRequisitionHold()"' in page
    assert ">暂不报料</button>" in page


def test_waiting_table_shows_component_demand_and_has_only_two_actions() -> None:
    waiting = _block(
        "<data-panel v-if=\"requisitionTab==='waiting'\"",
        "<div v-if=\"requisitionTab==='submitted' && requisitionItems.length\" class=\"reported-item-table-wrap\"",
    )
    header = re.search(r"<thead><tr>(.*?)</tr></thead>", waiting, flags=re.DOTALL)
    assert header
    assert header.group(1).count("<th>") == 6
    for label in (
        "客户 / 订单",
        "存货编码 / 产品",
        "订单 / 当前报料需求",
        "等候条件",
        "状态",
        "操作",
    ):
        assert f"<th>{label}</th>" in header.group(1)

    assert 'class="requisition-hold-table"' in waiting
    assert "requisition-hold-two-lines" in waiting
    assert "恢复待报料" in waiting
    assert ">修改</button>" in waiting
    assert "删除" not in waiting
    assert "requisitionHoldDemandText(row)" in waiting
    assert "requisitionHoldComponentText(component)" in waiting
    assert "row.requirement_warning" in waiting
    assert "row.can_restore_to_pending === false" in waiting
    assert ".requisition-hold-table { width: 100%; min-width: 0; table-layout: fixed; }" in INDEX
    assert "-webkit-line-clamp: 2" in INDEX


def test_hold_modal_keeps_daily_input_to_one_choice_and_no_reason_field() -> None:
    modal = _block(
        "<div v-else-if=\"modal.type === 'requisitionHold'\"",
        "<div v-else-if=\"modal.type === 'mergeSuggestions'\">",
    )

    assert "等上一批送完" in modal
    assert "指定日期" in modal
    assert 'type="date"' in modal
    assert "previous-batch-candidates" not in modal  # network logic stays in methods
    assert "没有可关联的上一批，请改用“指定日期”" in modal
    assert "原因" not in modal
    assert "备注" not in modal
    assert "is_recommended" in modal
    assert "v-model.number=\"requisitionHoldForm.previous_order_item_ids" in modal
    assert "requisitionHoldCandidateDifferences(candidate)" in modal
    assert "requisitionHoldDemandText(row)" in modal
    assert "requisitionHoldComponentText(component)" in modal
    assert 'class="requisition-hold-candidate-detail"' in modal
    assert ".requisition-hold-candidate-detail" in INDEX


def test_hold_requests_keep_batch_failures_visible_and_use_versioned_actions() -> None:
    methods = _block(
        "async autoReleaseReadyRequisitionHolds(signal=null)",
        "async loadReportedDocuments()",
    )

    assert 'axios.get("/api/requisition/holds"' in methods
    assert 'axios.post(' in methods
    assert '"/api/requisition/holds/auto-release"' in methods
    assert "previous-batch-candidates" in methods
    assert 'axios.post("/api/requisition/holds", {items:lines})' in methods
    assert "axios.put(`/api/requisition/holds/${this.requisitionHoldForm.id}`" in methods
    assert "expected_version:this.requisitionHoldForm.version" in methods
    assert "axios.post(`/api/requisition/holds/${row.id}/release`" in methods
    assert "{expected_version:Number(row.version || 1)}" in methods
    assert "data?.success_count" in methods
    assert "row.ok === false" in methods
    assert "失败明细仍保留在待报料" in methods
    assert "await this.loadRequisition()" in methods
    assert "await Promise.all([this.loadRequisition(), this.loadRequisitionHolds()])" not in methods
    save_modal = _block("async saveModal()", "async dispatchDelivery(row, options = {})")
    assert 'if (this.modal.type === "requisitionHold")' in save_modal
    assert "await this.saveRequisitionHold()" in save_modal
    assert "return true;" in save_modal


def test_dashboard_and_tab_use_authoritative_hold_summary() -> None:
    load_pending = _block(
        "async loadRequisition({skipAutoRelease=false}={})",
        "applyRequisitionHoldSummary(source)",
    )
    select_tab = _block("async selectRequisitionTab(tab)", "async refreshRequisitionTab()")

    assert 'axios.get("/api/requisition/holds", {' in load_pending
    assert "auto_released_hold_ids:autoReleasedIds" in load_pending
    assert 'if (tab === "waiting")' in select_tab
    assert "await this.loadRequisitionHolds()" in select_tab
    assert "dashboardRequisitionHoldSummary.total" in INDEX
    assert "dashboardRequisitionHoldSummary.due_count" in INDEX
    assert "pendingCard.waiting_count" in INDEX
    assert "pendingCard.waiting_due_count" in INDEX


def test_waiting_filters_pagination_and_order_labels_are_visible() -> None:
    page = _block(
        "<template v-else-if=\"activePage === 'requisition'\">",
        "<template v-else-if=\"activePage === 'incoming'\">",
    )

    for field in (
        'requisitionHoldFilters.customer_id',
        'requisitionHoldFilters.product_code',
        'requisitionHoldFilters.product_name',
        'requisitionHoldFilters.status',
        'requisitionHoldFilters.date_from',
        'requisitionHoldFilters.date_to',
    ):
        assert field in page
    assert ':total="requisitionHoldsTotal"' in page
    assert "预计恢复日期" in page
    assert "已等候 {{ row.waiting_days || 0 }} 天" in page
    assert "item.requisition_hold?.status === 'active'" in INDEX


def test_new_order_requisition_strategy_previews_and_requires_line_choices() -> None:
    order_modal = _block(
        '<div v-else-if="modal.type === \'order\'">',
        '<div v-else-if="modal.type === \'orderEdit\'">',
    )
    methods = _block("orderRequisitionPreviewCandidateText(candidate)", "validateOrderForm()")
    save_modal = _block('if (this.modal.type === "order")', 'if (this.modal.type === "orderEdit")')
    order_payload = _block(
        "const orderPayload = {",
        "const createdOrder = await this.saveNewOrder(orderPayload);",
    )

    assert "报料策略" in order_modal
    assert 'v-model="orderForm.requisition_strategy"' in order_modal
    assert 'value="normal"' in order_modal
    assert 'value="wait_previous_batch"' in order_modal
    assert "等候报料预检" in order_modal
    assert "orderRequisitionPreviewText(item)" in order_modal
    assert "requisition_previous_order_item_id" in order_modal
    assert "异常不会静默绑定" in order_modal
    assert 'axios.post("/api/requisition/order-entry/hold-preview"' in methods
    assert "lines:this.orderForm.items.map" in methods
    assert "finished_covered_quantity:finishedCoveredQuantity" in methods
    assert "selected_previous_order_item_id" in methods
    assert '"select_required", "blocked"' in methods
    for status in ("covered", "hold", "normal", "select_required", "blocked"):
        assert f'preview.status === "{status}"' in methods
    assert "requires_selection" in methods
    assert "applyOrderRequisitionFailure(error, message)" in methods
    assert "第${unresolved + 1}条报料策略存在异常" in methods
    assert "await this.ensureOrderRequisitionHoldPreview();" in save_modal
    assert "requisition_strategy" in save_modal
    assert "previous_batch_selections" in order_payload
    assert "Object.fromEntries" not in order_payload
    assert "previous_order_item_id:Number(item.requisition_previous_order_item_id || 0)" in order_payload
    assert "订单保存失败：" in save_modal


def test_inline_javascript_remains_syntax_valid() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for inline JavaScript syntax checks"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL)
        if script.strip()
    ]
    assert scripts
    for script in scripts:
        result = subprocess.run(
            [node, "--check"],
            input=script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=os.environ.copy(),
            check=False,
        )
        assert result.returncode == 0, result.stderr

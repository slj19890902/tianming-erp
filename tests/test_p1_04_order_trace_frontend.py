import re
import shutil
import subprocess
from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")
INCOMING = (
    Path(__file__).resolve().parents[1] / "static" / "incoming.html"
).read_text(encoding="utf-8")


def test_trace_entry_uses_exact_expanded_item() -> None:
    assert '@click="openOrderTrace(row,item)"' in INDEX
    assert (
        "`/api/orders/${orderId}/items/${itemId}/documents`"
        in INDEX
    )
    assert "const orderId = Number(order?.id || 0)" in INDEX
    assert "const itemId = Number(item?.id || 0)" in INDEX
    assert "{signal:controller.signal}" in INDEX
    assert 'openOrderTrace(group.orders[0]' not in INDEX
    assert '@click="openOrderDetail(group.orders[0])"' not in INDEX


def test_trace_modal_is_compact_and_does_not_claim_completion_date() -> None:
    start = INDEX.index("modal.type === 'orderTrace'")
    end = INDEX.index("modal.type === 'orderDetail'", start)
    trace_template = INDEX[start:end]
    assert "真实单据时间线" in trace_template
    assert "当前关联库存位置" in trace_template
    assert "序号" in trace_template
    assert "completion_date" not in trace_template
    assert "客户、客户单号、产品" not in trace_template


def test_trace_stage_detail_uses_exact_source_and_marks_reversed_history() -> None:
    assert "current_event_key" in INDEX
    assert "查看当前阶段" in INDEX
    assert "阶段详情" in INDEX
    assert (
        "`/api/orders/${orderId}/items/${itemId}"
        "/documents/${sourceType}/${sourceId}`"
        in INDEX
    )
    assert "const expectedTrace = this.orderTrace" in INDEX
    assert "该记录已撤销或冲销，仅作为历史查看" in INDEX


def test_trace_return_restores_order_workbench_context() -> None:
    method_start = INDEX.index("async openOrderTrace(order, item)")
    method_end = INDEX.index("traceEventTime(event)", method_start)
    methods = INDEX[method_start:method_end]
    assert "filters:{...this.filters}" in methods
    assert "orderPage:this.pages.orders" in methods
    assert "expandedOrders:{...this.expandedOrders}" in methods
    assert "scrollY:window.scrollY" in methods
    assert "this.pages.orders=context.orderPage" in methods
    assert "this.expandedOrders={...context.expandedOrders}" in methods
    assert "window.scrollTo(0,context?.scrollY || 0)" in methods


def test_admin_trace_rollback_is_sequential_and_uses_one_confirmation() -> None:
    assert "管理员下一步" in INDEX
    assert "traceNextRollbackEvent()" in INDEX
    assert 'this.user?.role !== "admin"' not in INDEX[
        INDEX.index("traceRollbackKind(event)") : INDEX.index(
            "traceEventTime(event)", INDEX.index("traceRollbackKind(event)")
        )
    ]
    start = INDEX.index("async rollbackTraceEvent(event)")
    end = INDEX.index("traceEventTime(event)", start)
    rollback_method = INDEX[start:end]
    assert "confirm(" in rollback_method
    assert "prompt(" not in rollback_method
    assert "订单追溯逐级回退（管理员一次确认）" in rollback_method
    assert "/api/production/completions/${event.source_id}/revert" in rollback_method
    assert "/api/incoming/receipt-items/${event.source_id}/revert" in rollback_method
    assert "/api/requisition/supplier-orders/${supplierOrderId}/void" in rollback_method
    assert "/api/requisition/items/${this.orderTrace.item.id}/cancel" in rollback_method
    assert "回退被阻止" in rollback_method


def test_all_visible_rollback_entries_use_admin_and_fixed_audit_reason() -> None:
    production_start = INDEX.index("async revertProductionCompletion(row)")
    production_end = INDEX.index("async loadIncoming()", production_start)
    production_method = INDEX[production_start:production_end]
    assert "prompt(" not in production_method
    assert "生产完工历史回退（管理员一次确认）" not in production_method
    assert "await axios.post(`/api/production/completions/${row.id}/revert`, {})" in production_method

    incoming_start = INDEX.index("async revertIncoming(row)")
    incoming_end = INDEX.index("async loadIncomingHistory()", incoming_start)
    incoming_method = INDEX[incoming_start:incoming_end]
    assert "prompt(" not in incoming_method
    assert "来料实收历史回退（管理员一次确认）" not in incoming_method
    assert "row._revert_idempotency_key" in incoming_method
    assert "idempotency_key:row._revert_idempotency_key" in incoming_method
    assert 'v-else-if="canAdmin" class="btn small danger"' in INDEX

    assert 'state.user?.role === "admin"' in INCOMING
    assert "revertReason" not in INCOMING
    assert "来料实收历史回退（管理员一次确认）" not in INCOMING
    assert "state.revertIdempotencyKeys.get(key)" in INCOMING
    assert "body: JSON.stringify({idempotency_key: revertKey})" in INCOMING


def test_inline_javascript_remains_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-04-order-trace.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    incoming_scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INCOMING, re.DOTALL
        )
        if script.strip() and "src=" not in script[:100]
    ]
    assert len(incoming_scripts) == 1
    incoming_target = tmp_path / "p1-25b-incoming.js"
    incoming_target.write_text(incoming_scripts[0], encoding="utf-8")
    incoming_result = subprocess.run(
        [node, "--check", str(incoming_target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert incoming_result.returncode == 0, incoming_result.stderr

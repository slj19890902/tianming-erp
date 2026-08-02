import re
import shutil
import subprocess
from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_trace_entry_uses_exact_expanded_item() -> None:
    assert '@click="openOrderTrace(row,item)"' in INDEX
    assert (
        "axios.get(`/api/orders/${order.id}/items/${item.id}/documents`)"
        in INDEX
    )
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
        "`/api/orders/${this.orderTrace.order.id}/items/${this.orderTrace.item.id}"
        "/documents/${sourceType}/${event.source_id}`"
        in INDEX
    )
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

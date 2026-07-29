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

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _orders_page() -> str:
    start = INDEX.index("activePage === 'orders'")
    end = INDEX.index("activePage === 'orders_legacy'", start)
    return INDEX[start:end]


def test_p1_08_orders_use_independent_scope_stage_and_order_customers() -> None:
    block = _orders_page()
    assert "order-status-tabs" in block
    assert "orderScopeTabs" in block
    assert 'v-model="filters.orderStage"' in block
    assert "orderCustomerOptions" in block
    assert "activeCustomerOptions" not in block
    assert "orderScope:\"active\"" in INDEX
    assert '{ label: "已作废/取消", value: "cancelled" }' in INDEX
    assert 'params.scope = this.filters.orderScope || "active";' in INDEX
    assert "params.stage = this.filters.orderStage;" in INDEX
    assert 'axios.get("/api/orders/customer-options"' in INDEX
    assert 'row.is_active === false ? "（已停用）" : ""' in INDEX


def test_p1_08_filter_changes_return_to_first_page_and_can_be_cleared() -> None:
    for marker in (
        "async refreshOrderFilters({ refreshCustomers=false } = {})",
        "this.pages.orders = 1;",
        "async applyOrderScope(scope)",
        "async applyOrderStage()",
        "async removeOrderFilter(key)",
        'this.filters.orderScope = "active";',
        'this.filters.orderStage = "";',
        "return Promise.all([this.loadOrderCustomerOptions(), this.loadOrders()]);",
    ):
        assert marker in INDEX


def test_p1_08_compact_pager_counts_filters_not_result_rows() -> None:
    assert 'props: ["page", "total", "pageSize", "always", "compact", "filterCount"]' in INDEX
    assert "已筛选 {{ filterCount == null ? 0 : filterCount }} 项" in INDEX
    assert ':filter-count="orderActiveFilterChips.length"' in INDEX
    assert "已筛选 {{ total }} 项" not in INDEX


def test_p1_08_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-08-order-filters.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

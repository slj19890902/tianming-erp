from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _delivery_page() -> str:
    start = INDEX.index("activePage === 'deliveries'")
    end = INDEX.index("activePage === 'finance'", start)
    return INDEX[start:end]


def test_delivery_page_has_one_compact_keyword_search() -> None:
    page = _delivery_page()
    assert page.count('placeholder="搜索送货单"') == 1
    assert 'v-model.trim="deliveryListFilters.keyword"' in page
    assert 'maxlength="5"' not in page
    for removed in (
        'placeholder="客户"',
        'placeholder="送货单号"',
        'placeholder="订单号"',
        'placeholder="客户单号"',
        'placeholder="存货编码"',
        'placeholder="产品名称"',
        'placeholder="规格"',
    ):
        assert removed not in page


def test_filterbar_keeps_compact_search_and_pager_while_details_are_explicit() -> None:
    page = _delivery_page()
    start = page.index('<div class="list-filterbar delivery-list-filterbar">')
    end = page.index('<div v-if="deliveryListState.initialLoading"', start)
    toolbar = page[start:end]
    compact_markers = [
        "deliveryListFilters.keyword",
        ">查询<",
        "deliveryListFilterVisible=!deliveryListFilterVisible",
        '<pager :page="pages.deliveries"',
    ]
    compact_positions = [toolbar.index(marker) for marker in compact_markers]
    assert compact_positions == sorted(compact_positions)

    advanced_start = toolbar.index(
        'v-if="deliveryListFilterVisible" class="delivery-detailed-filterbar delivery-advanced-filters"'
    )
    advanced = toolbar[advanced_start:]
    advanced_markers = [
        "deliveryListFilters.customer_id",
        "deliveryListFilters.date_from",
        "deliveryListFilters.date_to",
        "deliveryListFilters.status",
        "deliveryListFilters.return_status",
        "deliveryDetailedFilters.customer_po",
        "deliveryDetailedFilters.product_code",
        "deliveryDetailedFilters.product_name",
        ">查询详细条件<",
        ">清空详细条件<",
    ]
    positions = [advanced.index(marker) for marker in advanced_markers]
    assert positions == sorted(positions)
    assert ':filter-count="deliveryListFilterCount"' in toolbar
    assert ':page-size="deliveryListPageSize()"' in toolbar
    assert "pages.deliveries=$event; loadDeliveries()" in toolbar
    assert "deliveryListFilterVisible:false" in INDEX


def test_filter_model_and_desktop_layout_use_unified_contract() -> None:
    assert (
        'deliveryListFilters: { customer_id:"", keyword:"", date_from:"", '
        'date_to:"", status:"", return_status:"" }'
    ) in INDEX
    assert (
        'deliveryDetailedFilters: { customer_po:"", product_code:"", '
        'product_name:"" }'
    ) in INDEX
    assert ".delivery-list-filterbar .delivery-keyword-input" in INDEX
    assert "width:108px" in INDEX
    assert "@media (min-width: 1700px)" in INDEX
    assert ".delivery-list-filterbar { flex-wrap:nowrap; }" in INDEX
    assert ".delivery-list-filterbar .pager.top" in INDEX
    assert "margin-left:auto" in INDEX


def test_delivery_loader_still_uses_server_paging_and_all_current_filters() -> None:
    start = INDEX.index("async loadDeliveries() {")
    end = INDEX.index("async loadDeliveryPendingItems() {", start)
    loader = INDEX[start:end]
    assert "page: Number(this.pages.deliveries || 1)" in loader
    assert "page_size: Number(this.deliveryListPageSize())" in loader
    assert "Object.entries(this.deliveryListFilters || {})" in loader
    assert "Object.entries(this.deliveryDetailedFilters || {})" in loader
    assert 'axios.get("/api/deliveries", { params })' in loader
    assert "requestToken !== this.deliveryListState.request_token" in loader


def test_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-26b-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

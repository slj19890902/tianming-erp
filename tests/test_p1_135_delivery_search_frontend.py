from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _delivery_page() -> str:
    start = INDEX.index("activePage === 'deliveries'")
    end = INDEX.index("activePage === 'finance'", start)
    return INDEX[start:end]


def test_delivery_page_has_one_top_pager_and_no_subtitle() -> None:
    page = _delivery_page()
    assert page.count('class="pager top delivery-list-pager"') == 1
    assert page.count('<pager :page="pages.deliveries"') == 0
    assert "支持多订单合并、分批发货、打印及客户实收确认" not in page
    assert "第 {{ pages.deliveries }}/{{ deliveryListPages }} 页" in page


def test_detailed_search_is_collapsed_and_keeps_three_fields() -> None:
    page = _delivery_page()
    assert "deliveryDetailedSearchVisible" in page
    assert "详细查找" in page
    assert 'v-model.trim="deliveryDetailedFilters.customer_po"' in page
    assert 'v-model.trim="deliveryDetailedFilters.product_code"' in page
    assert 'v-model.trim="deliveryDetailedFilters.product_name"' in page
    assert "清空详细条件" in page
    assert "deliveryDetailedFilterCount" in page


def test_delivery_loader_uses_mode_specific_server_page_size_and_details() -> None:
    start = INDEX.index("async loadDeliveries() {")
    end = INDEX.index("async loadDeliveryPendingItems() {", start)
    loader = INDEX[start:end]
    assert "page_size: Number(this.deliveryListPageSize())" in loader
    assert "Object.entries(this.deliveryDetailedFilters || {})" in loader
    assert 'axios.get("/api/deliveries", { params })' in loader
    assert "requestToken !== this.deliveryListState.request_token" in loader
    assert "DELIVERY_LIST_PAGE_SIZE_STANDARD" in INDEX
    assert "DELIVERY_LIST_PAGE_SIZE_LARGE" in INDEX


def test_delivery_match_metadata_is_visible_without_color_only_signal() -> None:
    page = _delivery_page()
    assert "row.search_matches.matched_values" in page
    assert "deliverySearchFieldLabel" in page
    assert "命中" in page
    assert "deliverySearchItemFieldMatched" in page
    assert ".delivery-search-hit" in INDEX
    assert ".delivery-list-panel .table-wrap" in INDEX
    compact = INDEX.replace(" ", "")
    assert "max-height:none" in compact
    assert "overflow-y:visible" in compact

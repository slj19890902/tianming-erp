from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _orders_page() -> str:
    start = INDEX.index("activePage === 'orders'")
    end = INDEX.index("activePage === 'orders_legacy'", start)
    return INDEX[start:end]


def _board_requisition_page() -> str:
    start = INDEX.index("<template v-else>\n              <div class=\"page-head requisition-page-head", INDEX.index("activePage === 'requisition'"))
    end = INDEX.index("activePage === 'incoming'", start)
    return INDEX[start:end]


def test_order_conditions_clear_and_authoritative_pager_share_the_status_row() -> None:
    page = _orders_page()
    compact = page.split('<div class="order-filter-compact">', 1)[1].split(
        '<div v-if="showAdvancedFilter"', 1
    )[0]
    assert 'class="order-filter-footer"' in compact
    assert "orderActiveFilterChips.length" in compact
    assert "一键清空" in compact
    assert ':page-size="orderListPageSize()"' in compact
    assert 'class="order-filter-footer"' not in page.split('<div v-if="showAdvancedFilter"', 1)[1]
    assert ".order-filter-footer {" in INDEX
    footer_css = INDEX.split(".order-filter-footer {", 1)[1].split("}", 1)[0]
    assert "margin-top:0" in footer_css.replace(" ", "")
    order_pager = INDEX.split('<template v-if="$attrs[\'order-layout\']">', 1)[1].split(
        "<template v-else>", 1
    )[0]
    assert "筛选查找" not in order_pager
    assert order_pager.index("上一页") < order_pager.index("第 {{ page }}/{{ pages }} 页") < order_pager.index("下一页")


def test_reported_header_tabs_and_actions_share_one_compact_row() -> None:
    page = _board_requisition_page()
    head = page.split('<div class="page-head requisition-page-head requisition-board-head">', 1)[1].split(
        "</div>\n              <div v-if=\"requisitionInventoryActionBlocked()\"", 1
    )[0]
    for marker in (
        "纸板报料",
        "待报料",
        "已报料/已入库",
        "合并报料",
        "暂不报料",
        "库存补库",
        "等候报料",
        "包材报料",
        "刷新",
    ):
        assert marker in head
    assert "纸板订单报料、库存抵扣和库存补库" not in page


def test_reported_filter_print_actions_and_pager_share_the_top_tool_row() -> None:
    page = _board_requisition_page()
    top = page.split('class="list-filterbar reported-filterbar"', 1)[1].split(
        '<div v-if="requisitionTab===\'submitted\' && reportedAdvancedVisible"', 1
    )[0]
    for marker in (
        "reportedFilters.customer_id",
        "reportedFilters.report_length_mm",
        "reportedFilters.report_width_mm",
        "已选 <strong>{{ reportedSelectedItems().length }}</strong> 条",
        "清空已选",
        "打印待来料任务单",
        "打印产品标签",
        "refreshReportedItems",
        ':page-size="reportedItemPageSize()"',
    ):
        assert marker in top
    assert "逐明细打印" not in page
    assert "默认不选；勾选跨筛选、翻页保留" not in page
    assert "当前条件命中" not in page
    assert page.count(':page="pages.requisitionReported"') == 1


def test_reported_table_is_single_scroll_and_uses_requested_column_order() -> None:
    page = _board_requisition_page()
    table = page.split('aria-label="已报料逐明细紧凑表格"', 1)[1].split(
        'aria-label="已报料明细详情"', 1
    )[0]
    markers = [
        "报料长",
        "报料宽",
        "压线尺寸",
        "采购数量",
        "材质 / 楞型",
        "供应商",
        "客户简称 / 存货编码",
        "报料日期",
        "状态 / 操作",
    ]
    positions = [table.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert '@click="openReportedItemDetail(row)"' in table
    wrap_css = INDEX.split(".reported-item-table-wrap {", 1)[1].split("}", 1)[0]
    assert "max-height: none" in wrap_css
    assert "overflow-y: visible" in wrap_css
    dimension_css = INDEX.split(".reported-item-table .reported-item-dimension {", 1)[1].split("}", 1)[0]
    assert "text-align: left" in dimension_css


def test_reported_items_use_mode_specific_server_page_sizes() -> None:
    assert "REPORTED_ITEM_PAGE_SIZE_STANDARD = 12" in INDEX
    assert "REPORTED_ITEM_PAGE_SIZE_LARGE = 8" in INDEX
    assert "reportedItemPageSize(mode=this.uiMode)" in INDEX
    loader = INDEX.split("async loadReportedDocuments() {", 1)[1].split(
        "\n          reportedItemsRequestIsCurrent(controller", 1
    )[0]
    assert 'typeof this.reportedItemPageSize === "function"' in loader
    assert "? this.reportedItemPageSize()" in loader
    assert "page_size: effectivePageSize" in loader
    assert "Math.ceil(total / effectivePageSize)" in loader

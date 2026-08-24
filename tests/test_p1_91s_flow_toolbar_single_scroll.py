from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _between(start: str, end: str, offset: int = 0) -> str:
    begin = INDEX.index(start, offset)
    return INDEX[begin : INDEX.index(end, begin)]


def _board_requisition() -> str:
    start = '<div class="page-head requisition-page-head requisition-board-head">'
    return _between(start, '<template v-else-if="activePage === \'incoming\'">')


def _board_incoming() -> str:
    page = _between(
        '<template v-else-if="activePage === \'incoming\'">',
        '<template v-else-if="activePage === \'production\'">',
    )
    start = page.index('<div class="toolbar incoming-compact-head">')
    return page[start:]


def _production() -> str:
    return _between(
        '<template v-else-if="activePage === \'production\'">',
        '<template v-else-if="activePage === \'deliveries\'">',
    )


def test_primary_flow_pages_remove_the_generic_bottom_scroll_gap() -> None:
    assert ".main.primary-flow-main { padding-bottom: 0; }" in INDEX
    main = "'primary-flow-main': ['orders','requisition','incoming','production'].includes(activePage)"
    assert main in INDEX


def test_flow_tables_delegate_vertical_scrolling_to_the_main_page() -> None:
    assert ".table-wrap:has(> .incoming-compact-table) { max-height:none; overflow-x:hidden; overflow-y:visible; }" in INDEX
    shared = _between(
        ".table-wrap:has(> .requisition-pending-table),",
        ".incoming-compact-table th,.incoming-compact-table td",
    )
    for selector in (
        ".table-wrap:has(> .requisition-hold-table)",
        ".table-wrap:has(> .production-table)",
        ".table-wrap:has(> .production-history-table)",
    ):
        assert selector in shared
    assert "max-height:none" in shared
    assert "overflow-y:visible" in shared
    assert ".reported-item-table-wrap { max-height: none; overflow-x: auto; overflow-y: visible;" in INDEX


def test_paper_flow_headers_are_single_compact_command_bars_without_help_copy() -> None:
    requisition = _board_requisition()
    incoming = _board_incoming()
    production = _production()

    assert requisition.index("<h2>纸板报料</h2>") < requisition.index("待报料")
    assert requisition.index("待报料") < requisition.index("合并报料")
    assert "<p>" not in requisition.split("</div>", 1)[0]
    assert "requisition-board-head" in requisition

    assert incoming.index("纸板收料") < incoming.index("待入库")
    assert incoming.index("待入库") < incoming.index("批量确认入库")
    assert "<page-head" not in incoming
    assert "incoming-compact-head" in incoming

    assert production.index("<h2>生产确认</h2>") < production.index("待生产")
    assert production.index("待生产") < production.index("批量确认入库")
    assert "<page-head" not in production
    assert "production-compact-command-bar" in production
    assert "直接待送会进入一楼待送区" not in production


def test_each_board_tab_has_one_authoritative_top_pager() -> None:
    requisition = _board_requisition()
    incoming = _board_incoming()
    production = _production()

    assert requisition.count(':page="pages.requisitionPending"') == 1
    assert requisition.count(':page="pages.requisitionHolds"') == 1
    assert requisition.count(':page="pages.requisitionReported"') == 1
    assert incoming.count(':page="pages.incomingPending"') == 1
    assert incoming.count(':page="pages.incomingReceived"') == 1
    assert incoming.count(':page="pages.incomingHistory"') == 1
    assert production.count(':page="pages.productionPending"') == 1
    assert production.count(':page="pages.productionHistory"') == 1


def test_compact_flow_page_sizes_are_server_driven_and_mode_specific() -> None:
    for declaration in (
        "REPORTED_ITEM_PAGE_SIZE_STANDARD = 12",
        "REPORTED_ITEM_PAGE_SIZE_LARGE = 8",
        "REQUISITION_PENDING_PAGE_SIZE_STANDARD = 6",
        "REQUISITION_PENDING_PAGE_SIZE_LARGE = 4",
        "REQUISITION_HOLD_PAGE_SIZE_STANDARD = 8",
        "REQUISITION_HOLD_PAGE_SIZE_LARGE = 6",
        "PRODUCTION_PENDING_PAGE_SIZE_STANDARD = 6",
        "PRODUCTION_PENDING_PAGE_SIZE_LARGE = 4",
        "PRODUCTION_HISTORY_PAGE_SIZE_STANDARD = 8",
        "PRODUCTION_HISTORY_PAGE_SIZE_LARGE = 6",
    ):
        assert declaration in INDEX

    assert 'typeof this.requisitionPendingPageSize === "function"' in INDEX
    assert "? this.requisitionPendingPageSize()" in INDEX
    assert "const effectivePageSize = this.requisitionHoldPageSize();" in INDEX
    assert "page_size: effectivePageSize" in INDEX
    assert 'page_size: this.productionPendingPageSize()' in INDEX
    assert 'page_size: this.productionHistoryPageSize()' in INDEX


def test_ui_mode_change_reloads_the_visible_flow_page_only() -> None:
    method = _between(
        "async reloadOrdersForUiModeChange(previousMode, nextMode) {",
        "hasPermission(code) {",
    )
    assert 'this.activePage === "incoming"' in method
    assert 'this.activePage === "requisition"' in method
    assert 'this.activePage === "production"' in method
    assert "this.pages.incomingPending = 1" in method
    assert "this.pages.requisitionReported = 1" in method
    assert "this.pages.productionHistory = 1" in method

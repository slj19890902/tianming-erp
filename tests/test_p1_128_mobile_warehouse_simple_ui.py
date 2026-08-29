from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_mobile_bottom_navigation_is_capped_at_four_visible_items() -> None:
    assert 'data-page="more">更多' in MOBILE
    assert "if (entryIds.length <= 3)" in MOBILE
    assert "state.primaryEntryIds = primary.slice(0, 2)" in MOBILE
    assert "state.homeEntryIds = state.moreEntryIds.length" in MOBILE
    assert 'new Set(["home", ...entryIds' in MOBILE
    assert "--nav-count" in MOBILE


def test_mobile_warehouse_search_is_primary_and_map_is_explicit() -> None:
    search_page = MOBILE[MOBILE.index('id="searchPage"') : MOBILE.index('id="productionPage"')]
    assert search_page.index('id="searchInput"') < search_page.index('id="warehouseMapButton"')
    assert "浏览实测仓库地图（可选）" in search_page
    set_page = MOBILE[MOBILE.index("function setPage(page") : MOBILE.index("function showStatus")]
    assert "openWarehouseMap();" not in set_page


def test_mobile_low_frequency_entries_remain_permission_derived() -> None:
    assert 'id="moreEntryCards"' in MOBILE
    assert "state.moreEntryIds = entryIds.filter" in MOBILE
    assert "const entries = Array.isArray(state.shell?.entries)" in MOBILE
    assert "entryButton(entry)" in MOBILE


def test_warehouse_daily_navigation_stays_at_four_primary_actions() -> None:
    tabs = WAREHOUSE[WAREHOUSE.index('<div class="tabs">') : WAREHOUSE.index('<details class="warehouse-secondary">')]
    assert tabs.count('data-tab=') == 3
    assert 'id="mapStocktakeLink"' in tabs
    assert "库存台账" in tabs
    assert "库存流水" in tabs
    assert "模具位置" in tabs
    assert "盘点上架" in tabs
    assert "低频管理与高级台账" in WAREHOUSE

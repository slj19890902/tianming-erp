from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")
WAREHOUSE = (PROJECT_ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_p1_16b_keeps_business_pages_behind_one_order_warehouse_workbench() -> None:
    assert 'key:"workbench", label:"订单与仓库"' in INDEX
    assert '{key:"orders",label:"订单"}' in INDEX
    assert '{key:"requisition",label:"报料"}' in INDEX
    assert '{key:"incoming",label:"来料入库"}' in INDEX
    assert '{key:"warehouse",label:"仓库地图"}' in INDEX
    assert "isMenuActive(menuKey)" in INDEX
    assert "async goMenu(menuKey)" in INDEX
    assert "this.pageAllowed(page.key)" in INDEX


def test_p1_16b_delegates_floor_switching_to_the_embedded_twin_header() -> None:
    target = "/warehouse.html?embedded=1&floor=3F&view=2d"
    assert target in INDEX
    assert "warehouseTwinFloor: \"3F\"" in INDEX
    assert "默认先看真实三楼地图；切换其它模块不会丢失当前仓库页面。" not in INDEX
    assert "warehouse-floor-card" not in INDEX
    assert "厂房楼层导视" not in INDEX


def test_p1_16b_preserves_existing_warehouse_state_and_manual_refresh() -> None:
    assert "if (!this.warehouseFrameUrl)" in INDEX
    assert "refreshWarehouseFrame()" in INDEX
    assert "this.warehouseFrameRevision += 1" in INDEX
    load_page = INDEX.split("async loadPage(page", 1)[1].split("refreshCurrent()", 1)[0]
    cache_guard = load_page.split("if (!force && this.pageCacheFresh(page)) {", 1)[1].split("}", 1)[0]
    assert "return;" in cache_guard

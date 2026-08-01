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


def test_p1_16b_defaults_to_real_floor_three_map_without_fake_first_floor() -> None:
    target = "/warehouse.html?embedded=1&tab=locations&location_view=floor3"
    assert target in INDEX
    assert 'class="warehouse-floor-card future"' in INDEX
    assert "台账未建立 / 建设中" in INDEX
    for label in ("一楼", "台账未建立 / 建设中"):
        assert label in INDEX
    assert '"locations"' in WAREHOUSE
    assert 'requestedLocationView=params.get("location_view")' in WAREHOUSE
    assert 'requestedLocationView==="floor3"' in WAREHOUSE


def test_p1_16b_preserves_existing_warehouse_state_and_manual_refresh() -> None:
    assert "if (!this.warehouseFrameUrl)" in INDEX
    assert "refreshWarehouseFrame()" in INDEX
    assert "this.warehouseFrameRevision += 1" in INDEX
    assert "if (!force && this.pageCacheFresh(page)) return;" in INDEX

from pathlib import Path


WAREHOUSE_HTML = (
    Path(__file__).resolve().parents[1] / "static" / "warehouse.html"
).read_text(encoding="utf-8")


def test_warehouse_map_has_floor_switch_and_keeps_floor3_entry() -> None:
    assert 'data-location-view="floor3" type="button">仓库地图' in WAREHOUSE_HTML
    assert 'data-factory-floor="1F"' in WAREHOUSE_HTML
    assert 'data-factory-floor="3F"' in WAREHOUSE_HTML
    assert 'id="factoryFloor1Content"' in WAREHOUSE_HTML
    assert 'id="floor3FloorContent"' in WAREHOUSE_HTML
    assert "async function switchFactoryFloor(floorCode)" in WAREHOUSE_HTML
    assert 'state.factoryMap.activeFloor="3F"' not in WAREHOUSE_HTML
    assert 'factoryMap:{activeFloor:"3F"' in WAREHOUSE_HTML


def test_floor1_map_uses_read_only_api_and_real_dxf_layers() -> None:
    assert '/api/warehouse/factory-maps/floors/1F' in WAREHOUSE_HTML
    assert "function renderFactoryMapSvg()" in WAREHOUSE_HTML
    assert "function selectFactoryMapZone(zoneCode)" in WAREHOUSE_HTML
    for kind in ("wall", "zone", "machine", "rack", "fire"):
        assert f"kind-{kind}" in WAREHOUSE_HTML
    assert "当前只显示 DXF 真实图层和业务区域" in WAREHOUSE_HTML
    assert "不生成栈板位、不绑定库存" in WAREHOUSE_HTML
    assert "没有独立边界；当前不生成推测轮廓" in WAREHOUSE_HTML
    assert "仅限临时周转；室外受天气影响，不得作为长期库存区" in WAREHOUSE_HTML


def test_floor1_map_does_not_add_inventory_write_paths() -> None:
    loader = WAREHOUSE_HTML.split("async function loadFactoryFloor1Map(){", 1)[1].split(
        "// 三楼货位", 1
    )[0]
    assert 'method:"POST"' not in loader
    assert 'method:"PUT"' not in loader
    assert 'method:"DELETE"' not in loader
    assert "pallet_location" not in loader

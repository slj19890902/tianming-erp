from pathlib import Path


WAREHOUSE_HTML = (
    Path(__file__).resolve().parents[1] / "static" / "warehouse.html"
).read_text(encoding="utf-8")


def test_warehouse_ledger_routes_map_access_to_the_single_measured_entry() -> None:
    assert '<a class="btn" href="/warehouse.html" target="_top">实测仓库地图</a>' in WAREHOUSE_HTML
    assert 'data-tab="locations"' not in WAREHOUSE_HTML
    assert 'if(requestedTab==="locations"){openMeasuredWarehouseMap(params);return}' in WAREHOUSE_HTML
    assert 'data-factory-floor="1F"' in WAREHOUSE_HTML
    assert 'data-factory-floor="3F"' in WAREHOUSE_HTML
    assert 'id="factoryFloor1Content"' in WAREHOUSE_HTML
    assert 'id="floor3FloorContent"' in WAREHOUSE_HTML
    assert "async function switchFactoryFloor(floorCode)" in WAREHOUSE_HTML
    assert 'state.factoryMap.activeFloor="3F"' not in WAREHOUSE_HTML
    assert 'factoryMap:{activeFloor:"3F"' in WAREHOUSE_HTML
    assert ".floor3-command-title .actions .btn:first-of-type{display:none}" not in WAREHOUSE_HTML


def test_floor1_map_uses_read_only_api_and_real_dxf_layers() -> None:
    assert '/api/warehouse/factory-maps/floors/1F' in WAREHOUSE_HTML
    assert "function renderFactoryMapSvg()" in WAREHOUSE_HTML
    assert "function selectFactoryMapZone(zoneCode)" in WAREHOUSE_HTML
    for kind in ("wall", "zone", "machine", "rack", "fire"):
        assert f"kind-{kind}" in WAREHOUSE_HTML
    assert "这是一张员工查看用的只读平面图" in WAREHOUSE_HTML
    assert "不生成栈板位、不绑定库存" in WAREHOUSE_HTML
    assert "位置边界还没有画出，当前不会显示推测位置" in WAREHOUSE_HTML
    assert "仅限临时周转；室外受天气影响，不得作为长期库存区" in WAREHOUSE_HTML


def test_floor1_map_renders_an_employee_friendly_professional_floor_plan() -> None:
    assert "一楼工厂平面图" in WAREHOUSE_HTML
    assert "function factoryMapOrthogonalPoints(primitive,data)" in WAREHOUSE_HTML
    assert "function factoryMapOpeningSvg(data,primitive)" in WAREHOUSE_HTML
    assert "function factoryMapStaffLabelSvg(data,item)" in WAREHOUSE_HTML
    assert "function factoryMapStockTypeLabel(value)" in WAREHOUSE_HTML
    assert "factory-map-door-swing" in WAREHOUSE_HTML
    assert "factory-map-window-line" in WAREHOUSE_HTML
    assert "kind-mold_storage" in WAREHOUSE_HTML
    assert "模具货架" in WAREHOUSE_HTML
    assert "超大模具靠墙区" in WAREHOUSE_HTML
    assert "item.display_text" in WAREHOUSE_HTML
    assert "图纸版本 ${data.map_version} · 尺寸单位：毫米 · 已校验" in WAREHOUSE_HTML


def test_floor1_racks_open_a_read_only_front_elevation_locator() -> None:
    assert 'selectedRackId:""' in WAREHOUSE_HTML
    assert 'selectedRackSlot:""' in WAREHOUSE_HTML
    assert "function selectFactoryMapRack(rackId)" in WAREHOUSE_HTML
    assert "function selectFactoryMapRackSlot(level,bay)" in WAREHOUSE_HTML
    assert "function renderFactoryMapRackFront(rack)" in WAREHOUSE_HTML
    assert 'data-rack-id="${h(primitive.id)}"' in WAREHOUSE_HTML
    assert "factory-map-rack-front" in WAREHOUSE_HTML
    assert "factory-map-rack-cell" in WAREHOUSE_HTML
    assert "pointer-events:none" in WAREHOUSE_HTML
    assert "从左第${bay}段" in WAREHOUSE_HTML
    assert "横向分段为地图定位示意" in WAREHOUSE_HTML
    assert "不绑定具体模具档案" in WAREHOUSE_HTML


def test_floor1_fixed_equipment_uses_noninteractive_25d_assets_and_racks_stay_on_top() -> None:
    assert "function factoryMapMachineAssetSvg(data,primitive)" in WAREHOUSE_HTML
    assert "factory-map-machine-image" in WAREHOUSE_HTML
    assert "machine:4" in WAREHOUSE_HTML
    assert "rack:5" in WAREHOUSE_HTML
    assert "machine_asset_opacity" in WAREHOUSE_HTML
    assert "opacity=\"${opacity}\"" in WAREHOUSE_HTML
    assert "data-machine-id" not in WAREHOUSE_HTML
    assert "固定设备，仅记录位置，禁止堆放栈板" in WAREHOUSE_HTML
    assert "上方货运电梯停用" in WAREHOUSE_HTML
    assert "货架为主，机器仅作定位参考" in WAREHOUSE_HTML
    assert "绿色框是可临时放 2 托的栈板区" in WAREHOUSE_HTML


def test_floor1_map_does_not_add_inventory_write_paths() -> None:
    loader = WAREHOUSE_HTML.split("async function loadFactoryFloor1Map(){", 1)[1].split(
        "// 三楼货位", 1
    )[0]
    assert 'method:"POST"' not in loader
    assert 'method:"PUT"' not in loader
    assert 'method:"DELETE"' not in loader
    assert "pallet_location" not in loader

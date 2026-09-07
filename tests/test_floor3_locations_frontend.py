from pathlib import Path
import json
import re
import subprocess


WAREHOUSE_HTML = (Path(__file__).resolve().parents[1] / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_location_management_routes_all_maps_to_the_measured_twin() -> None:
    assert WAREHOUSE_HTML.count('data-tab="locations"') == 0
    assert 'data-tab="floor3_locations"' not in WAREHOUSE_HTML
    assert '<a class="btn" href="/warehouse.html" target="_top">实测仓库地图</a>' in WAREHOUSE_HTML
    assert 'if(requestedTab==="locations"){openMeasuredWarehouseMap(params);return}' in WAREHOUSE_HTML
    assert 'if(tab==="locations"){openMeasuredWarehouseMap();return}' in WAREHOUSE_HTML
    assert 'async function switchLocationView(view)' in WAREHOUSE_HTML
    assert 'function canManageLocations(){return !state.readOnly&&state.user?.role==="admin"}' in WAREHOUSE_HTML
    assert 'if(view==="ledger"&&!canManageLocations())' in WAREHOUSE_HTML
    assert 'state.tab==="locations"&&state.locationView==="floor3"' in WAREHOUSE_HTML
    assert 'id="floor3LocationSection"' in WAREHOUSE_HTML
    assert 'id="locationSection"' in WAREHOUSE_HTML
    assert 'id="floor3AreaFilter"' in WAREHOUSE_HTML
    assert 'id="floor3OccupancyFilter"' in WAREHOUSE_HTML
    assert 'id="floor3KeywordFilter"' in WAREHOUSE_HTML
    assert 'page_size:"500"' in WAREHOUSE_HTML
    assert 'FLOOR3_AREA_OPTIONS=[' in WAREHOUSE_HTML
    for area_code in ("A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2", "CD1", "D1", "D2", "DE1", "E1", "E2", "E3", "SEMI-011", "F1", "F2", "F3", "F4"):
        assert f'["{area_code}"' in WAREHOUSE_HTML
    assert '["E4","E4 区"]' not in WAREHOUSE_HTML
    assert '"F12"' in WAREHOUSE_HTML and '"F34"' in WAREHOUSE_HTML


def test_new_floor3_slot_refreshes_area_and_global_overview() -> None:
    block = WAREHOUSE_HTML.split("async function addFloor3LayoutSlot(){", 1)[1].split(
        "async function toggleFloor3LayoutSlot", 1
    )[0]
    assert "/api/warehouse/floor3/layout/areas/" in block
    assert "await refreshFloor3LocationData()" in block
    assert '$("floor3NewSlotCode").value=""' in block
    assert "并已同步到全局平面图" in block
    refresh = WAREHOUSE_HTML.split("async function refreshFloor3LocationData(){", 1)[1].split(
        "async function loadFloor3MoveLocations", 1
    )[0]
    assert 'state.floor3.mapLocations=state.readOnly?(overview.items||[]).filter(row=>row.position_status==="mapped"&&row.layout):(overview.items||[])' in refresh
    assert "renderFloor3Plan()" in refresh
    assert "await loadFloor3Locations(true)" in refresh
    loader = WAREHOUSE_HTML.split("async function loadFloor3Locations(throwOnError=false){", 1)[1].split(
        "async function refreshFloor3LocationData", 1
    )[0]
    assert "if(throwOnError)throw error" in loader


def test_floor3_api_contract_paths_and_payloads_are_wired() -> None:
    for path in (
        "/api/warehouse/floor3/locations?",
        "/api/warehouse/floor3/locations/${locationId}",
        "/api/warehouse/floor3/product-candidates?",
        '"/api/warehouse/pallets"',
        "/api/warehouse/pallets/${pallet.id}/items",
        "/api/warehouse/pallets/${palletId}/move",
        "/api/warehouse/pallets/${palletId}/clear",
        "/api/warehouse/pallets/${palletId}/relocation-flag",
    ):
        assert path in WAREHOUSE_HTML
    assert "const locationId=Number(state.floor3.selectedLocationId)" in WAREHOUSE_HTML
    assert "location_id:locationId" in WAREHOUSE_HTML
    assert "pallet_code:" in WAREHOUSE_HTML
    assert "items})" in WAREHOUSE_HTML
    assert 'body:JSON.stringify({expected_version:expectedVersion,item})' in WAREHOUSE_HTML
    assert "function floor3ExpectedVersion(palletId)" in WAREHOUSE_HTML
    assert "expected_version:expectedVersion" in WAREHOUSE_HTML
    assert "to_location_id:Number(targetId)" in WAREHOUSE_HTML
    assert "expected_version:expectedVersion,to_location_id:Number(targetId)" in WAREHOUSE_HTML
    assert "expected_target_layout_version:targetLayoutVersion" in WAREHOUSE_HTML
    assert "confirmed:true" in WAREHOUSE_HTML
    assert "idempotency_key:createIdempotencyKey()" in WAREHOUSE_HTML
    assert "needs_relocation:Boolean(needsRelocation)" in WAREHOUSE_HTML
    assert "placement_confirmed:true" in WAREHOUSE_HTML
    for path in (
        "/api/warehouse/floor3/layout/areas/${encodeURIComponent(state.floor3.selectedAreaCode)}/slots",
        "/api/warehouse/floor3/layout/areas/${encodeURIComponent(state.floor3.selectedAreaCode)}",
        "/slots/${locationId}/${enable?\"enable\":\"disable\"}",
    ):
        assert path in WAREHOUSE_HTML


def test_floor3_view_and_execute_gates_are_enforced() -> None:
    assert 'hasPermission("warehouse.view")' in WAREHOUSE_HTML
    assert 'hasPermission("warehouse.execute")' in WAREHOUSE_HTML
    assert "if(!canOperate())return" in WAREHOUSE_HTML
    assert 'class="panel hidden operate-only"' in WAREHOUSE_HTML
    assert "!canOperate()" in WAREHOUSE_HTML


def test_floor3_restricted_pallet_never_renders_write_controls() -> None:
    detail_block = WAREHOUSE_HTML.split("function renderFloor3Detail(){", 1)[1].split(
        "function closeFloor3Detail(){", 1
    )[0]
    assert "function floor3PalletRestricted(pallet)" in WAREHOUSE_HTML
    assert "当前位置已占用（客户范围受限）" in WAREHOUSE_HTML
    assert "位置内容受客户范围权限保护" in WAREHOUSE_HTML
    assert "pallet&&!restricted&&canOperate()?" in detail_block
    assert "pallet&&!restricted&&canOperate()?" in detail_block


def test_floor3_candidate_selection_is_explicit_and_pending_is_supported() -> None:
    assert "row.candidate=null" in WAREHOUSE_HTML
    assert "selectFloor3BindCandidate" in WAREHOUSE_HTML
    assert "selectFloor3AddCandidate" in WAREHOUSE_HTML
    assert "点击选择" in WAREHOUSE_HTML
    assert "if(!row.pending&&!row.candidate)return null" in WAREHOUSE_HTML
    assert 'match_status:pending?"pending":"matched"' in WAREHOUSE_HTML
    assert "暂存为待匹配" in WAREHOUSE_HTML
    assert "candidates[0]" not in WAREHOUSE_HTML


def test_floor3_cards_mark_aisle_and_cross_type_relocation() -> None:
    assert 'code==="F12"||code==="F34"' in WAREHOUSE_HTML
    assert "过道临放" in WAREHOUSE_HTML
    assert "需归位" in WAREHOUSE_HTML
    assert "floor3NeedsRelocation" in WAREHOUSE_HTML
    assert "标记待归位" in WAREHOUSE_HTML
    assert "确认已归位" in WAREHOUSE_HTML
    assert "function confirmFloor3Placement(palletId)" in WAREHOUSE_HTML
    assert "过道临放货物必须先移动到固定货位" in WAREHOUSE_HTML


def test_floor3_is_mobile_card_layout_and_has_unlimited_rows() -> None:
    assert "floor3-location-grid" in WAREHOUSE_HTML
    assert "@media(max-width:560px)" in WAREHOUSE_HTML
    assert "floor3-toolbar>*{width:100%!important" in WAREHOUSE_HTML
    assert "增加一行产品" in WAREHOUSE_HTML
    assert "function addFloor3BindRow()" in WAREHOUSE_HTML
    assert "state.floor3.bindRows.push" in WAREHOUSE_HTML
    bind_add = WAREHOUSE_HTML.split("function addFloor3BindRow()", 1)[1].split(
        "function removeFloor3BindRow", 1
    )[0]
    assert "Math.min" not in bind_add


def test_floor3_uses_metric_world_geometry_without_the_v11_raster_asset() -> None:
    assert "floor3_warehouse_plan_v11.png" not in WAREHOUSE_HTML
    assert 'id="floor3PlanCanvas"' in WAREHOUSE_HTML
    assert 'id="floor3PlanHotspots"' in WAREHOUSE_HTML
    for geometry_name in (
        "FLOOR3_WORLD",
        "FLOOR3_ZONES",
        "FLOOR3_RACK_DEFS",
        "FLOOR3_MAIN_AISLE",
    ):
        assert f"const {geometry_name}=" in WAREHOUSE_HTML

    geometry = WAREHOUSE_HTML.split("const FLOOR3_WORLD=", 1)[1].split(
        "const FLOOR3_MAIN_AISLE", 1
    )[0]
    for metric_field in ("x:", "y:", "w:", "h:"):
        assert metric_field in geometry
    assert "单位为米" in WAREHOUSE_HTML
    assert "function floor3WorldRect(rect)" in WAREHOUSE_HTML
    assert "function floor3RackRect(definition)" in WAREHOUSE_HTML
    assert "floor3RectStyle(FLOOR3_MAIN_AISLE)" in WAREHOUSE_HTML
    assert "1.90m" in WAREHOUSE_HTML

    rack_geometry = WAREHOUSE_HTML.split("const FLOOR3_RACK_DEFS=", 1)[1].split(
        "const ", 1
    )[0]
    for rack_code in ("F2", "F3", "F4"):
        assert f'"{rack_code}"' in rack_geometry
    assert "FLOOR3_MAIN_AISLE" in WAREHOUSE_HTML


def test_floor3_fit_mode_has_no_scroll_at_100_percent() -> None:
    assert "body.floor3-mode{height:100vh;overflow:hidden}" in WAREHOUSE_HTML
    assert re.search(
        r"\.floor3-plan-scroll(?:\:not\(\.zoomed\))?\{[^}]*overflow(?:-x)?\s*:\s*hidden",
        WAREHOUSE_HTML,
    )
    assert "state.floor3.planZoom===100" in WAREHOUSE_HTML
    assert 'scroller.classList.toggle("zoomed",state.floor3.planZoom>100)' in WAREHOUSE_HTML
    assert ".floor3-plan-scroll.zoomed{overflow:auto}" in WAREHOUSE_HTML
    assert ".floor3-plan-canvas{min-width:680px}" not in WAREHOUSE_HTML


def test_floor3_map_slot_selection_renders_in_fixed_right_rail() -> None:
    click_block = WAREHOUSE_HTML.split("async function floor3OpenMapSlot", 1)[1].split(
        "async function floor3OpenLocationFromMap", 1
    )[0]
    assert "state.floor3.mapPopoverLocationId=locationId" in click_block
    assert "renderFloor3Plan()" in click_block
    assert "selectFloor3Area" not in click_block
    assert 'id="floor3OverviewDefault"' in WAREHOUSE_HTML
    assert 'id="floor3OverviewSelection"' in WAREHOUSE_HTML
    assert "function floor3OverviewSelectionHtml(row)" in WAREHOUSE_HTML
    assert "function clearFloor3OverviewSelection()" in WAREHOUSE_HTML

    marker_block = WAREHOUSE_HTML.split("function floor3SlotMarker", 1)[1].split(
        "function floor3RackLevel", 1
    )[0]
    assert "mapPopoverLocationId" in marker_block
    assert 'class="floor3-map-pallet-card"' not in marker_block
    summary_block = WAREHOUSE_HTML.split("function floor3PalletSummary", 1)[1].split(
        "function floor3SlotMarker", 1
    )[0]
    for field in ("customer_name", "customer", "inventory_code", "product_name", "quantity", "styleCount"):
        assert field in summary_block
    for label in ("客户", "存货编码", "产品名称", "款式数"):
        assert label in summary_block
    assert "floor3QuantityHtml(item)" in summary_block
    assert "floor3-item-quantity" in WAREHOUSE_HTML


def test_floor3_f2_f3_f4_rack_cells_are_clickable_but_not_pallet_drag_endpoints() -> None:
    zones_block = WAREHOUSE_HTML.split("const FLOOR3_ZONES=[", 1)[1].split(
        "];", 1
    )[0]
    rack_block = WAREHOUSE_HTML.split("const FLOOR3_RACK_DEFS=", 1)[1].split(
        "const ", 1
    )[0]
    for rack_code in ("F2", "F3", "F4"):
        assert re.search(rf'id:"{rack_code}"[^}}]*allowPallets:false', zones_block)
        assert f'zoneId:"{rack_code}"' in rack_block

    marker_block = WAREHOUSE_HTML.split("function floor3SlotMarker", 1)[1].split(
        "function renderFloor3AreaSlots", 1
    )[0]
    assert "onclick" in marker_block
    assert "floor3OpenMapSlot" in marker_block
    assert "draggable" in marker_block
    assert 'floor3IsRack(row)?"rack-cell":""' in marker_block

    drag_start = WAREHOUSE_HTML.split("function floor3StartPalletDrag", 1)[1].split(
        "function ", 1
    )[0]
    drop = WAREHOUSE_HTML.split("function floor3DropPallet", 1)[1].split(
        "function renderFloor3MoveConfirmation", 1
    )[0]
    assert "floor3CanMovePallet(row)" in drag_start
    assert "event.preventDefault();return" in drag_start
    assert "floor3CanMovePallet(source)" in drop
    assert "floor3CanReceivePallet(target)" in drop
    assert "function floor3IsRack(row)" in WAREHOUSE_HTML
    assert "!floor3IsRack(row)" in WAREHOUSE_HTML


def test_floor3_overview_collapses_f_subareas_into_one_entry() -> None:
    assert 'const FLOOR3_F_AREA_CODES=["F1","F2","F3","F4","F12","F34"]' in WAREHOUSE_HTML
    assert 'const FLOOR3_F_GROUP_HOTSPOT=["F",50.4,1.8]' in WAREHOUSE_HTML
    hotspots = WAREHOUSE_HTML.split("const FLOOR3_PLAN_HOTSPOTS=", 1)[1].split(
        "const FLOOR3_F_GROUP_HOTSPOT", 1
    )[0]
    assert "filter(zone=>!FLOOR3_F_AREA_CODES.includes(zone.id))" in hotspots
    plan = WAREHOUSE_HTML.split("function renderFloor3Plan(){", 1)[1].split(
        "function floor3OverviewSelectionHtml", 1
    )[0]
    assert "floor3AreaStats(\"F\")" in plan
    assert "selectFloor3Area('F',true)" in plan
    assert "!FLOOR3_F_AREA_CODES.includes(floor3AreaCode(row))" in plan
    assert 'class="${fClasses}" data-floor3-area="F"' in plan
    assert 'floor3-hotspot-stats' not in plan.split("const fArea=", 1)[1].split(
        "const overviewSlots", 1
    )[0]
    assert ".floor3-f-entry{" in WAREHOUSE_HTML
    rail = WAREHOUSE_HTML.split("function renderFloor3OverviewRail", 1)[1].split(
        "async function selectFloor3Area", 1
    )[0]
    assert '["F 区",["F"]]' in rail
    for f_subarea in ('"F1"', '"F2"', '"F3"', '"F4"', '"F12"', '"F34"'):
        assert f_subarea not in rail
    assert "codes.map(code=>" in rail


def test_floor3_f_group_uses_rack_level_visualization_without_horizontal_scroll() -> None:
    assert "function floor3RackLevel(row)" in WAREHOUSE_HTML
    assert "function floor3FRackCell(row,bottom=false)" in WAREHOUSE_HTML
    assert "function floor3FRackCard(areaCode,rows)" in WAREHOUSE_HTML
    assert "function floor3FTemporaryHtml(rows)" in WAREHOUSE_HTML
    assert "function floor3FGroupHtml(rows)" in WAREHOUSE_HTML
    assert "function floor3FSecondaryHtml(rows)" in WAREHOUSE_HTML
    assert 'areaCode==="F1"?[2,1]:[3,2,1]' in WAREHOUSE_HTML
    assert 'bottom?"底部栈板货物"' in WAREHOUSE_HTML
    assert '["F12","F34"]' in WAREHOUSE_HTML
    assert 'floor3FRackCard("F1",rows)' in WAREHOUSE_HTML
    assert 'floor3FRackCard("F2",rows)' in WAREHOUSE_HTML
    assert 'floor3FRackCard("F3",rows)' in WAREHOUSE_HTML
    assert 'floor3FRackCard("F4",rows)' in WAREHOUSE_HTML
    assert 'floor3FRackCard("F1",rows)}<div class="floor3-f-temporary">${floor3FTemporaryHtml(rows)}' in WAREHOUSE_HTML
    assert ".floor3-rack-frame{" in WAREHOUSE_HTML
    assert "transform:rotateX(2deg) rotateY(-2deg)" in WAREHOUSE_HTML
    assert ".floor3-area-slot-map.floor3-f-group-map{height:auto!important;min-height:0;overflow-y:auto;overflow-x:hidden" in WAREHOUSE_HTML
    assert 'area==="F"?floor3FGroupHtml(rows)' in WAREHOUSE_HTML
    assert '$("floor3FSecondaryPanel").innerHTML=floor3FSecondaryHtml(rows)' in WAREHOUSE_HTML
    assert "F 区概览" not in WAREHOUSE_HTML
    assert ".floor3-area-focus.floor3-f-mode{grid-template-columns:repeat(2,minmax(0,1fr))}" in WAREHOUSE_HTML
    assert 'classList.toggle("floor3-f-mode",areaCode==="F")' in WAREHOUSE_HTML


def test_floor3_f_detail_moves_away_from_the_selected_rack_without_duplicate_panels() -> None:
    assert WAREHOUSE_HTML.count('id="floor3AreaLeftContextHost"') == 1
    assert WAREHOUSE_HTML.count('id="floor3FSecondaryPanel"') == 1
    assert WAREHOUSE_HTML.count('id="floor3ContextPanels"') == 1
    assert WAREHOUSE_HTML.count('id="floor3DetailPanel"') == 1
    assert "function floor3DetailSide()" in WAREHOUSE_HTML
    detail_side = WAREHOUSE_HTML.split("function floor3DetailSide(){", 1)[1].split(
        "function floor3PlaceContextPanels", 1
    )[0]
    assert 'area==="F3"||area==="F4"' in detail_side
    placement = WAREHOUSE_HTML.split("function floor3PlaceContextPanels(){", 1)[1].split(
        "function floor3AreaStats", 1
    )[0]
    assert 'leftSide?$("floor3AreaLeftContextHost"):$("floor3AreaDetailRail")' in placement
    assert "host.appendChild(panels)" in placement
    assert '$("floor3AreaSlotMap").classList.toggle("hidden",leftSide)' in placement
    assert '$("floor3FSecondaryPanel").classList.toggle("hidden"' in placement
    assert "floor3PlaceContextPanels()" in WAREHOUSE_HTML.split(
        "function renderFloor3Detail(){", 1
    )[1].split("function closeFloor3Detail", 1)[0]
    area_rule = WAREHOUSE_HTML.split(".floor3-area-focus{", 1)[1].split("}", 1)[0]
    assert "overflow-x:hidden" in area_rule


def test_floor3_d1_uses_current_measured_positions_instead_of_legacy_special_layout() -> None:
    assert 'if(areaCode==="D1")return 23' in WAREHOUSE_HTML
    render = WAREHOUSE_HTML.split("function renderFloor3AreaSlots(){", 1)[1].split(
        "function renderFloor3LayoutEditor", 1
    )[0]
    assert 'area==="F"?floor3FGroupHtml(rows)' in render
    assert "floor3AreaBackdropHtml(area)" in render
    assert "rows.map(row=>floor3SlotMarker(row,true))" in render
    assert 'area==="D1"?floor3D1LayoutHtml(rows)' not in render


def test_floor3_current_map_replaces_e4_with_semi_finished_area() -> None:
    assert '["SEMI-011","SEMI-011 半成品堆放区"]' in WAREHOUSE_HTML
    assert 'if(areaCode==="SEMI-011")return 4' in WAREHOUSE_HTML
    assert '["半成品区",["SEMI-011"]]' in WAREHOUSE_HTML
    render = WAREHOUSE_HTML.split("function renderFloor3AreaSlots(){", 1)[1].split(
        "function renderFloor3LayoutEditor", 1
    )[0]
    assert 'area==="E4"||area==="DE1"' not in render
    assert "floor3VerticalStructureHtml(area,rows)" not in render


def test_floor3_capacity_copy_separates_actual_enabled_and_theoretical_capacity() -> None:
    assert "function floor3TheoreticalCapacity" in WAREHOUSE_HTML
    capacity_block = WAREHOUSE_HTML.split("function floor3AreaStats", 1)[1].split(
        "function ", 1
    )[0]
    assert "enabled" in capacity_block
    assert "theoretical" in capacity_block
    assert "row.is_active!==false" in capacity_block
    area_detail = WAREHOUSE_HTML.split("function renderFloor3AreaSlots", 1)[1].split(
        "function renderFloor3LayoutEditor", 1
    )[0]
    assert "stats.enabled" in area_detail
    assert "stats.theoretical" in area_detail
    for label in ("实际启用容量", "理论容量"):
        assert label in WAREHOUSE_HTML


def test_floor3_area_can_add_and_disable_or_enable_physical_positions() -> None:
    assert "function addFloor3LayoutSlot()" in WAREHOUSE_HTML
    assert "function toggleFloor3LayoutSlot(locationId,enable)" in WAREHOUSE_HTML
    assert 'id="floor3NewSlotCode"' in WAREHOUSE_HTML
    assert 'onclick="addFloor3LayoutSlot()"' in WAREHOUSE_HTML
    assert "is_active===false" in WAREHOUSE_HTML
    assert "/api/warehouse/floor3/layout/areas/${encodeURIComponent(state.floor3.selectedAreaCode)}/slots" in WAREHOUSE_HTML
    assert "/api/warehouse/floor3/layout/slots/${locationId}/${enable?\"enable\":\"disable\"}" in WAREHOUSE_HTML
    assert "expected_version:expectedVersion" in WAREHOUSE_HTML


def test_floor3_plan_is_the_initial_workspace_and_cards_are_lazy() -> None:
    assert 'id="floor3Workspace" class="floor3-workspace hidden"' in WAREHOUSE_HTML
    assert 'id="floor3WorkspaceTitle">请选择区域<' in WAREHOUSE_HTML
    assert "请先在平面图上点击一个区域" in WAREHOUSE_HTML
    assert "function floor3HasWorkspaceFilter()" in WAREHOUSE_HTML
    assert '$("floor3LocationCards").innerHTML=(hasWorkspace?rows:[])' in WAREHOUSE_HTML
    assert '$("floor3LocationCards").classList.toggle("hidden",!hasWorkspace||areaFocus)' in WAREHOUSE_HTML


def test_floor3_map_selection_and_search_stay_in_sync() -> None:
    selection = WAREHOUSE_HTML.split("async function selectFloor3Area", 1)[1].split(
        "async function clearFloor3Area", 1
    )[0]
    assert '$("floor3AreaFilter").value=state.floor3.selectedAreaCode' in selection
    assert '$("floor3KeywordFilter").value=""' in selection
    assert '$("floor3OccupancyFilter").value=""' in selection
    assert '$("floor3AreaFilter").onchange=event=>selectTwinOperationalArea(event.target.value)' in WAREHOUSE_HTML
    assert "function floor3MatchedAreas()" in WAREHOUSE_HTML
    assert 'searching&&matched.has(code)?"matched":""' in WAREHOUSE_HTML
    assert "function setFloor3PlanZoom(value,anchor=null)" in WAREHOUSE_HTML
    assert 'id="floor3PlanZoomIn"' in WAREHOUSE_HTML


def test_floor3_visual_workspace_has_command_nav_canvas_and_detail_columns() -> None:
    for element_id in (
        "floor3FilterPanel",
        "floor3AreaNavigator",
        "floor3PlanCanvas",
        "floor3OverviewSelection",
        "floor3AreaNavToggle",
        "floor3FullscreenToggle",
        "floor3LegendToggle",
        "floor3DefaultView",
    ):
        assert f'id="{element_id}"' in WAREHOUSE_HTML
    assert "仓库可视化库存" in WAREHOUSE_HTML
    assert "操作记录" in WAREHOUSE_HTML
    assert ".floor3-overview-stage{grid-template-columns:minmax(190px,220px) minmax(0,1fr) minmax(270px,300px)" in WAREHOUSE_HTML
    assert ".floor3-overview-stage.area-nav-collapsed" in WAREHOUSE_HTML
    assert "function toggleFloor3AreaNavigator()" in WAREHOUSE_HTML
    assert "function toggleFloor3Fullscreen()" in WAREHOUSE_HTML
    assert "function toggleFloor3Legend()" in WAREHOUSE_HTML


def test_floor3_search_is_debounced_filterable_and_ignores_stale_responses() -> None:
    assert 'id="floor3CustomerFilter"' in WAREHOUSE_HTML
    assert '$("floor3KeywordFilter").oninput=scheduleFloor3Search' in WAREHOUSE_HTML
    assert "function scheduleFloor3Search()" in WAREHOUSE_HTML
    loader = WAREHOUSE_HTML.split("async function loadFloor3Locations(throwOnError=false){", 1)[1].split(
        "async function refreshFloor3LocationData", 1
    )[0]
    assert 'customer_id:customerId' in loader
    assert "const requestId=++state.floor3.searchRequestId" in loader
    assert "if(requestId!==state.floor3.searchRequestId)return" in loader
    assert "function renderFloor3SearchResults()" in WAREHOUSE_HTML
    assert "async function focusFloor3SearchResult(locationId)" in WAREHOUSE_HTML
    assert "floor3-search-pulse" in WAREHOUSE_HTML
    assert 'classList.toggle("is-searching",searching)' in WAREHOUSE_HTML


def test_floor3_move_mode_is_explicit_and_stale_controls_cannot_move_inventory() -> None:
    assert 'id="floor3ModeBadge"' in WAREHOUSE_HTML
    assert 'id="floor3EditNotice"' in WAREHOUSE_HTML
    assert "async function toggleFloor3MoveMode()" in WAREHOUSE_HTML
    assert 'state.floor3.moveMode=!state.floor3.moveMode' in WAREHOUSE_HTML
    assert "function floor3CanMoveSelectedPallet(row)" in WAREHOUSE_HTML
    assert "function floor3CanMovePallet(row){return Boolean(state.floor3.moveMode&&floor3CanMoveSelectedPallet(row))}" in WAREHOUSE_HTML
    assert "移动栈板模式" in WAREHOUSE_HTML
    assert "调整平面图布局" in WAREHOUSE_HTML
    assert "if(state.floor3.detail)renderFloor3Detail()" in WAREHOUSE_HTML
    assert 'if(!floor3Active){document.body.classList.remove("floor3-area-mode","floor3-workspace-mode");state.floor3.moveMode=false' in WAREHOUSE_HTML


def test_floor3_canvas_supports_fit_reset_wheel_zoom_and_blank_area_pan() -> None:
    assert '$("floor3DefaultView").onclick=resetFloor3View' in WAREHOUSE_HTML
    assert "async function resetFloor3View()" in WAREHOUSE_HTML
    assert "function floor3HandleWheel(event)" in WAREHOUSE_HTML
    assert "function floor3StartPan(event)" in WAREHOUSE_HTML
    assert "function floor3MovePan(event)" in WAREHOUSE_HTML
    assert "function floor3EndPan()" in WAREHOUSE_HTML
    assert "canvasRect=canvas.getBoundingClientRect()" in WAREHOUSE_HTML
    assert 'event.target.closest("button,input,select")' in WAREHOUSE_HTML


def test_floor3_dynamic_select_options_are_html_escaped() -> None:
    assert 'function options(select,items,placeholder){select.innerHTML=`<option value="">${h(placeholder)}</option>`' in WAREHOUSE_HTML
    assert '<option value="${h(x.id)}">${h(x.label)}</option>' in WAREHOUSE_HTML


def test_floor3_plan_and_detail_layout_are_responsive() -> None:
    assert re.search(
        r"\.floor3-plan-scroll(?:\:not\(\.zoomed\))?\{[^}]*overflow(?:-x)?\s*:\s*hidden",
        WAREHOUSE_HTML,
    )
    assert ".floor3-plan-scroll.zoomed{overflow:auto}" in WAREHOUSE_HTML
    assert ".floor3-plan-canvas{position:relative;width:100%" in WAREHOUSE_HTML
    assert "state.floor3.planZoom=Math.max(100,Math.min(180,value))" in WAREHOUSE_HTML
    assert 'scroller.classList.toggle("zoomed",state.floor3.planZoom>100)' in WAREHOUSE_HTML
    assert "heightLimit=availableHeight>0?availableHeight:availableWidth/ratio" in WAREHOUSE_HTML
    assert "baseWidth=Math.min(availableWidth,heightLimit*ratio)" in WAREHOUSE_HTML
    assert "body.floor3-mode{height:100vh;overflow:hidden}" in WAREHOUSE_HTML
    assert "body.floor3-mode #floor3MapPanel{display:flex;flex:1;min-height:0" in WAREHOUSE_HTML
    assert ".floor3-plan-canvas{min-width:680px}" not in WAREHOUSE_HTML
    assert ".floor3-detail-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(280px,1fr)" in WAREHOUSE_HTML
    assert ".floor3-detail-grid,.floor3-area-focus{grid-template-columns:1fr}" in WAREHOUSE_HTML


def test_floor3_overview_uses_full_page_landscape_layout() -> None:
    assert "const FLOOR3_DISPLAY_ROTATED=true" in WAREHOUSE_HTML
    assert ".floor3-plan-canvas{aspect-ratio:47.4/27.15" in WAREHOUSE_HTML
    assert 'class="floor3-overview-stage"' in WAREHOUSE_HTML
    assert 'id="floor3OverviewStats"' in WAREHOUSE_HTML
    assert 'id="floor3OverviewZones"' in WAREHOUSE_HTML
    assert "function renderFloor3OverviewRail(matched,searching)" in WAREHOUSE_HTML
    assert "body.floor3-mode{height:100vh;overflow:hidden}" in WAREHOUSE_HTML
    assert "body.floor3-mode header,body.floor3-mode #locationViewTabs{display:none}" in WAREHOUSE_HTML
    assert re.search(
        r"body\.floor3-mode \.wrap\{[^}]*max-width:none[^}]*height:100vh",
        WAREHOUSE_HTML,
    )
    assert ".floor3-overview-stage{grid-template-columns:minmax(190px,220px) minmax(0,1fr) minmax(270px,300px)" in WAREHOUSE_HTML
    assert "body.floor3-mode .floor3-overview-rail{overflow:auto;padding:8px}" in WAREHOUSE_HTML
    assert "body.floor3-mode .floor3-overview-zones{display:block}" in WAREHOUSE_HTML
    assert ".floor3-overview-selection{height:100%;overflow-y:auto;overflow-x:hidden}" in WAREHOUSE_HTML


def test_floor3_landscape_rotation_preserves_saved_source_coordinates() -> None:
    assert "function floor3RotateLocalLayout(layout)" in WAREHOUSE_HTML
    assert "function floor3UnrotateLocalLayout(layout)" in WAREHOUSE_HTML
    assert "FLOOR3_DISPLAY_ROTATED?floor3RotateLocalLayout(source):source" in WAREHOUSE_HTML
    assert "FLOOR3_DISPLAY_ROTATED?floor3UnrotateLocalLayout(moved):moved" in WAREHOUSE_HTML
    assert "const ratio=FLOOR3_DISPLAY_ROTATED?FLOOR3_OVERVIEW_WORLD.h/FLOOR3_OVERVIEW_WORLD.w" in WAREHOUSE_HTML


def test_floor3_area_outlines_are_passive_and_use_separate_corridor_entries() -> None:
    zones = WAREHOUSE_HTML.split("const FLOOR3_ZONES=[", 1)[1].split(
        "];", 1
    )[0]
    f_area_codes = {"F1", "F2", "F3", "F4", "F12", "F34"}
    assert len([code for code in re.findall(r'id:"([A-Z0-9-]+)"', zones) if code not in f_area_codes]) == 16

    plan = WAREHOUSE_HTML.split("function renderFloor3Plan(){", 1)[1].split(
        "function floor3OverviewSelectionHtml", 1
    )[0]
    non_f_areas = plan.split("const areas=", 1)[1].split(
        "const [fCode", 1
    )[0]
    assert 'outlineClasses=["floor3-plan-hotspot"' in non_f_areas
    assert 'entryClasses=["floor3-area-entry"' in non_f_areas
    assert "entry=floor3AreaEntryAnchor(left,top,width)" in non_f_areas
    assert '<div class="${outlineClasses}" data-floor3-area-outline="${code}"' in non_f_areas
    assert 'aria-hidden="true"></div><button type="button" class="${entryClasses}"' in non_f_areas
    assert 'data-floor3-area-entry="${code}"' in non_f_areas
    assert 'onclick="selectFloor3Area(\'${code}\',true)"' in non_f_areas
    assert "floor3-hotspot-code" not in non_f_areas
    assert "floor3-hotspot-stats" not in non_f_areas

    outline = non_f_areas.split("return `", 1)[1].split("<button", 1)[0]
    assert "onclick=" not in outline
    assert re.search(r"\.floor3-plan-hotspot\{[^}]*pointer-events:none", WAREHOUSE_HTML)
    assert re.search(r"\.floor3-area-entry\{[^}]*position:absolute", WAREHOUSE_HTML)


def test_floor3_map_overlays_physical_slots_and_keeps_slot_clicks_out_of_area_focus() -> None:
    assert 'id="floor3PlanHotspots" class="floor3-plan-hotspots"' in WAREHOUSE_HTML
    assert "function floor3LocationLayout(row)" in WAREHOUSE_HTML
    for layout_field in ("left_pct", "top_pct", "width_pct", "height_pct", "z_index", "version", "source_type"):
        assert layout_field in WAREHOUSE_HTML
    assert "function floor3SlotMarker(row,areaFocus=false)" in WAREHOUSE_HTML
    assert "function floor3MarkerLayout(row,areaFocus)" in WAREHOUSE_HTML
    assert "area.left+(layout.left_pct/100)*area.width" in WAREHOUSE_HTML
    assert "floor3-overview-selection" in WAREHOUSE_HTML
    assert "async function floor3OpenMapSlot(event,locationId)" in WAREHOUSE_HTML
    assert "当前位置已占用（客户范围受限）" in WAREHOUSE_HTML
    assert "<b>占用</b>" in WAREHOUSE_HTML
    summary = WAREHOUSE_HTML.split("function floor3PalletSummary(row)", 1)[1].split(
        "function floor3SlotMarker", 1
    )[0]
    assert "item.inventory_code||item.product_code" in summary
    assert "floor3QuantityHtml(item)" in summary
    assert "<b>合计：" not in summary
    overview_click = WAREHOUSE_HTML.split("function floor3OpenMapSlot", 1)[1].split(
        "function floor3AllowPalletDrop", 1
    )[0]
    assert "event.stopPropagation()" in overview_click
    assert 'if(state.floor3.viewMode==="areaFocus"){await openFloor3Location(locationId);return}' in overview_click
    assert "state.floor3.mapPopoverLocationId=locationId" in overview_click
    direct_click = WAREHOUSE_HTML.split("function floor3OpenMapSlot", 1)[1].split(
        "async function floor3OpenLocationFromMap", 1
    )[0]
    assert "selectFloor3Area" not in direct_click
    assert 'state.floor3.viewMode="areaFocus"' not in overview_click


def test_floor3_drag_requires_confirmation_and_rejects_invalid_targets_client_side() -> None:
    drop_block = WAREHOUSE_HTML.split("function floor3DropPallet(event,targetId){", 1)[1].split(
        "function renderFloor3MoveConfirmation", 1
    )[0]
    assert "floor3CanMovePallet(source)||!floor3CanReceivePallet(target)||!pallet" in drop_block
    assert "state.floor3.pendingMove={source,target,pallet}" in drop_block
    assert "api(" not in drop_block
    assert "function cancelFloor3MapMove(){state.floor3.pendingMove=null" in WAREHOUSE_HTML
    assert '"地图拖拽移动栈板"' in WAREHOUSE_HTML
    assert "if(error.status===409)" in WAREHOUSE_HTML
    assert "await refreshFloor3LocationData()" in WAREHOUSE_HTML
    assert WAREHOUSE_HTML.count("state.floor3.pendingMove=null;renderFloor3MoveConfirmation()") >= 2


def test_floor3_area_focus_and_admin_layout_edit_are_separate_modes() -> None:
    assert 'id="floor3FilterPanel"' in WAREHOUSE_HTML
    assert 'id="floor3MapPanel"' in WAREHOUSE_HTML
    assert 'id="floor3AreaFocus"' in WAREHOUSE_HTML
    assert 'id="floor3AreaSlotMap"' in WAREHOUSE_HTML
    assert 'id="floor3LayoutEditToggle" class="btn admin-only"' in WAREHOUSE_HTML
    assert 'id="floor3AreaLayoutEditToggle" class="btn admin-only hidden"' in WAREHOUSE_HTML
    assert "state.floor3.viewMode=areaCode?\"areaFocus\":\"overview\"" in WAREHOUSE_HTML
    assert '$("floor3MapPanel").classList.toggle("hidden",areaFocus)' in WAREHOUSE_HTML
    assert '$("floor3FilterPanel").classList.remove("hidden")' in WAREHOUSE_HTML
    assert '$("floor3Workspace").classList.toggle("hidden",!hasWorkspace)' in WAREHOUSE_HTML
    assert '$("floor3AreaLayoutEditToggle").classList.toggle("hidden",!areaFocus||!floor3CanEditLayout()||state.floor3.selectedAreaCode==="F")' in WAREHOUSE_HTML
    assert "function floor3CanEditLayout(){return !state.readOnly&&state.user?.role===\"admin\"}" in WAREHOUSE_HTML
    assert "function floor3StartLayoutDrag" in WAREHOUSE_HTML
    assert "function floor3SetAreaBackdrop(areaCode)" in WAREHOUSE_HTML
    assert "target.dataset.areaCode=zone.id" in WAREHOUSE_HTML
    assert "floor3AreaBackdropHtml" in WAREHOUSE_HTML
    assert "当前登记数量合计" not in WAREHOUSE_HTML
    assert "点击左侧具体货位" in WAREHOUSE_HTML
    assert 'if(state.floor3.viewMode==="areaFocus")renderFloor3AreaSlots()' in WAREHOUSE_HTML
    assert "保存布局不会改变库存数量" in WAREHOUSE_HTML
    assert "占用货位不能停用" in WAREHOUSE_HTML
    assert "function addFloor3LayoutSlot()" in WAREHOUSE_HTML
    assert "location_name:code,...floor3DefaultNewSlotLayout()" in WAREHOUSE_HTML
    assert "location_id:Number(id),expected_version:Number(layout.version)" in WAREHOUSE_HTML
    assert "/api/warehouse/floor3/layout/slots/${locationId}/${enable?\"enable\":\"disable\"}" in WAREHOUSE_HTML
    assert "expected_version:expectedVersion" in WAREHOUSE_HTML
    assert 'include_inactive:floor3CanEditLayout()?"true":""' in WAREHOUSE_HTML


def test_floor3_layout_drag_can_return_to_origin_and_reset_unsaved_changes() -> None:
    assert 'ondragover="floor3AllowLayoutDrop(event)"' in WAREHOUSE_HTML
    assert 'ondrop="floor3DropLayout(event)"' in WAREHOUSE_HTML
    assert "layoutDrag:null" in WAREHOUSE_HTML
    assert "function floor3AllowLayoutDrop(event)" in WAREHOUSE_HTML
    assert "function floor3DropLayout(event)" in WAREHOUSE_HTML
    drop_block = WAREHOUSE_HTML.split("function floor3DropLayout(event){", 1)[1].split(
        "function floor3EndLayoutDrag", 1
    )[0]
    assert "floor3LayoutDropDisplay(display,pointerLeft,pointerTop,drag.offsetLeftPct,drag.offsetTopPct)" in drop_block
    helper = WAREHOUSE_HTML.split("function floor3LayoutDropDisplay", 1)[1].split("function floor3DropLayout", 1)[0]
    assert "pointerLeft-offsetLeftPct" in helper
    assert "pointerTop-offsetTopPct" in helper
    assert "100-display.width_pct" in helper
    assert "100-display.height_pct" in helper
    assert "renderFloor3AreaSlots()" in drop_block
    assert "function floor3EndLayoutDrag(){state.floor3.layoutDrag=null}" in WAREHOUSE_HTML
    assert "function resetFloor3LayoutDrafts()" in WAREHOUSE_HTML
    assert "撤销本区未保存调整" in WAREHOUSE_HTML
    assert "已恢复到本次编辑前的位置" in WAREHOUSE_HTML
    assert "function floor3EndLayoutDrag(event,locationId)" not in WAREHOUSE_HTML


def test_floor3_b1_r03_layout_drag_numeric_round_trip() -> None:
    functions = []
    for name in ("floor3RotateLocalLayout", "floor3UnrotateLocalLayout", "floor3LayoutDropDisplay"):
        match = re.search(rf"^\s*(function {name}\([^\n]+)$", WAREHOUSE_HTML, re.MULTILINE)
        assert match, f"missing JavaScript helper {name}"
        functions.append(match.group(1))
    script = "\n".join(functions) + r"""
const original={left_pct:50,top_pct:28.5714,width_pct:50,height_pct:14.2857,z_index:4};
const display=floor3RotateLocalLayout(original);
const offsetLeft=display.width_pct/2,offsetTop=display.height_pct/2;
const moved=floor3LayoutDropDisplay(display,70,60,offsetLeft,offsetTop);
const returned=floor3LayoutDropDisplay(moved,display.left_pct+offsetLeft,display.top_pct+offsetTop,offsetLeft,offsetTop);
const source=floor3UnrotateLocalLayout(returned);
console.log(JSON.stringify({display,moved,returned,source}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    for key in ("left_pct", "top_pct", "width_pct", "height_pct"):
        assert abs(payload["source"][key] - {"left_pct": 50, "top_pct": 28.5714, "width_pct": 50, "height_pct": 14.2857}[key]) < 0.0001
    assert 0 <= payload["moved"]["left_pct"] <= 100 - payload["moved"]["width_pct"]
    assert 0 <= payload["moved"]["top_pct"] <= 100 - payload["moved"]["height_pct"]


def test_floor3_slot_markers_do_not_apply_retired_e4_offsets() -> None:
    marker_block = WAREHOUSE_HTML.split("function floor3SlotMarker(row,areaFocus=false){", 1)[1].split(
        "function floor3RackLevel", 1
    )[0]
    assert 'floor3AreaCode(row)==="E4"' not in marker_block
    assert '"overview-e4-rack"' not in marker_block
    assert "floor3MarkerLayout(row,areaFocus)" in marker_block


def test_floor3_clear_uses_one_impact_confirmation_and_move_filters_empty_active() -> None:
    clear_block = WAREHOUSE_HTML.split("async function clearFloor3Pallet(palletId){", 1)[1].split(
        "async function setFloor3Relocation", 1
    )[0]
    assert clear_block.count("confirm(") == 1
    assert "操作人、时间和原货位会自动记录" in clear_block
    assert "floor3CanReceivePallet(row)" in WAREHOUSE_HTML


def test_floor3_move_targets_are_loaded_independently_from_browse_filters() -> None:
    loader = WAREHOUSE_HTML.split("async function loadFloor3MoveLocations(){", 1)[1].split(
        "function renderFloor3Locations(){", 1
    )[0]
    assert 'location-candidates?inventory_type=finished&empty_only=true&pallet_storage_only=true&include_hierarchy=false&published_only=true' in loader
    assert '$("floor3AreaFilter")' not in loader
    assert '$("floor3KeywordFilter")' not in loader
    assert '$("floor3OccupancyFilter")' not in loader
    assert "filter(floor3CanReceivePallet)" in loader
    assert "state.floor3.moveLocations.filter" in WAREHOUSE_HTML
    assert "state.floor3.locations.filter(row=>Number(row.id)!==Number(currentId)" not in WAREHOUSE_HTML


def test_finished_in_is_one_step_and_refreshes_inventory_once() -> None:
    assert '<button id="openInForm" class="btn primary operate-only">入成品仓</button>' in WAREHOUSE_HTML
    assert 'tab==="finished"?"入成品仓":"入半成品仓"' in WAREHOUSE_HTML
    assert "入成品仓（一步完成）" in WAREHOUSE_HTML
    assert "做完马上送货时无需办理入仓" in WAREHOUSE_HTML
    assert "只选客户、产品、数量和实际库位" in WAREHOUSE_HTML
    assert "可选：修改日期、来源或备注" in WAREHOUSE_HTML
    assert "function inventoryFloorLabel(row)" in WAREHOUSE_HTML
    assert "row.warehouse_floor" in WAREHOUSE_HTML
    save_block = WAREHOUSE_HTML.split("async function saveFinished(e){", 1)[1].split(
        "async function saveSemi", 1
    )[0]
    assert "/api/warehouse/finished/manual-in" in save_block
    assert 'runWarehouseMutation("manual-finished"' in save_block
    assert 'if(await loadLots()===false)throw new Error("库存列表刷新失败")' in save_block
    assert '"成品入库成功"' in save_block
    assert "三楼已绑定" in WAREHOUSE_HTML
    assert "正式成品库存" in WAREHOUSE_HTML
    assert "现场盘点快照" in WAREHOUSE_HTML
    assert "pallet&&!restricted&&canOperate()?" in WAREHOUSE_HTML


def test_finished_in_uses_customer_scoped_debounced_product_candidates() -> None:
    finished_form = WAREHOUSE_HTML.split('<form id="finishedForm"', 1)[1].split("</form>", 1)[0]
    for element_id in (
        "fgCustomer",
        "fgProductKeyword",
        "fgProductCandidates",
        "fgProductSelected",
    ):
        assert f'id="{element_id}"' in finished_form
    assert 'id="fgProduct" type="hidden"' in finished_form
    assert finished_form.index('id="fgCustomer"') < finished_form.index('id="fgProductKeyword"')
    assert not re.search(r'<select[^>]*id="fgProduct(?:[" ])', finished_form)

    assert '$("fgCustomer").onchange=resetFinishedProductSearch' in WAREHOUSE_HTML
    assert '$("fgProductKeyword").oninput=scheduleFinishedProductSearch' in WAREHOUSE_HTML
    schedule = WAREHOUSE_HTML.split("function scheduleFinishedProductSearch(){", 1)[1].split(
        "async function searchFinishedProductCandidates", 1
    )[0]
    assert "clearFinishedProductSelection()" in schedule
    assert "setTimeout(searchFinishedProductCandidates,260)" in schedule

    search = WAREHOUSE_HTML.split("async function searchFinishedProductCandidates(){", 1)[1].split(
        "function updateFlutes", 1
    )[0]
    assert "customer_id:customerId" in search
    assert "q:keyword" in search
    assert "/api/warehouse/floor3/product-candidates?" in search
    assert "requestId!==state.finishedProductSearchRequestId" in search


def test_finished_product_candidate_requires_manual_selection_and_shows_specification() -> None:
    render = WAREHOUSE_HTML.split("function renderFinishedProductCandidates(){", 1)[1].split(
        "function selectFinishedProductCandidate", 1
    )[0]
    assert "product_name||item.name" in render
    assert "item.specification||\"\"" in render
    assert "selectFinishedProductCandidate(${index})" in render


def test_inventory_lot_actions_stay_compact_and_keep_distinct_workflows() -> None:
    assert 'id="lotTable"' in WAREHOUSE_HTML
    assert '.table-wrap.finished-lot-wrap{overflow-x:hidden}' in WAREHOUSE_HTML
    assert '.finished-lot-table{table-layout:fixed;white-space:normal}' in WAREHOUSE_HTML
    assert 'classList.toggle("finished-lot-table",finished)' in WAREHOUSE_HTML
    assert '.finished-lot-table th:nth-child(4){width:10%}' in WAREHOUSE_HTML
    assert '.finished-lot-table th:nth-child(5){width:8%}' in WAREHOUSE_HTML
    assert '.finished-lot-table th:nth-child(6){width:12%}' in WAREHOUSE_HTML
    assert '.finished-lot-table th:nth-child(11){width:9.5%;text-align:right}' in WAREHOUSE_HTML
    assert 'justify-content:flex-end' in WAREHOUSE_HTML
    assert '.lot-customer{display:-webkit-box;' in WAREHOUSE_HTML
    assert '-webkit-line-clamp:2' in WAREHOUSE_HTML
    assert '.lot-batch,.lot-status{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' in WAREHOUSE_HTML
    assert '<span class="lot-batch" title="${h(row.lot_number)}"><b>${h(row.lot_number)}</b></span>' in WAREHOUSE_HTML
    assert '<span class="lot-material">${h(d.material_code||"-")}</span>' in WAREHOUSE_HTML
    assert '<span class="lot-flute">${h(d.flute_type||"-")}</span>' in WAREHOUSE_HTML
    assert '.lot-material,.lot-flute{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' in WAREHOUSE_HTML
    assert '.lot-actions{display:flex;align-items:center;align-content:center;justify-content:flex-end;gap:3px;flex-wrap:wrap' in WAREHOUSE_HTML
    assert '.lot-actions .btn{padding:3px 5px;font-size:11px;line-height:1.2;white-space:nowrap}' in WAREHOUSE_HTML
    assert '<td class="lot-actions">${actionButtons(row)}</td>' in WAREHOUSE_HTML

    actions = WAREHOUSE_HTML.split("function actionButtons(row){", 1)[1].split(
        "async function openProductAssignments", 1
    )[0]
    finished_return = actions.index("return detail+label+staging+edit;")
    assert 'onclick="openLotDetail(${row.id})">详情</button>' in actions[:finished_return]
    assert '>转入库位</button>' in actions[:finished_return]
    assert 'onclick="openLotEditor(${row.id})">编辑</button>' in actions[:finished_return]
    assert ">转通用</button>" not in actions
    assert actions.index(">冻结</button>") > finished_return
    assert actions.index(">报损</button>") > finished_return
    assert actions.index(">报废</button>") > finished_return

    assert 'FINISHED_LOT_COLUMN_WIDTHS_KEY="warehouseFinishedLotColumnWidthsV1"' in WAREHOUSE_HTML
    assert 'function installFinishedLotColumnResizers()' in WAREHOUSE_HTML
    assert 'className="lot-column-resizer"' in WAREHOUSE_HTML
    assert '拖动调整相邻列宽；双击恢复默认列宽' in WAREHOUSE_HTML
    assert 'startLeft+startRight' in WAREHOUSE_HTML
    assert 'pairWidth-leftWidth' in WAREHOUSE_HTML
    assert 'localStorage.setItem(FINISHED_LOT_COLUMN_WIDTHS_KEY' in WAREHOUSE_HTML
    assert 'localStorage.removeItem(FINISHED_LOT_COLUMN_WIDTHS_KEY)' in WAREHOUSE_HTML
    assert 'if(finished)installFinishedLotColumnResizers()' in WAREHOUSE_HTML
    assert "fgProductSelected" in WAREHOUSE_HTML

    save = WAREHOUSE_HTML.split("async function saveFinished(e){", 1)[1].split(
        "async function saveSemi", 1
    )[0]
    assert "if(!productId)" in save
    assert "点击选择匹配候选" in save
    assert 'product_id:productId' in save


def test_mold_status_and_finished_age_rows_do_not_render_explanation_blocks() -> None:
    assert ".compact-lines-3{display:-webkit-box;max-height:4.05em;overflow:hidden;" in WAREHOUSE_HTML
    assert "-webkit-line-clamp:3" in WAREHOUSE_HTML
    assert 'return `<tr class="compact-ledger-row">${common}' in WAREHOUSE_HTML
    assert '<td>${w}</td><td class="lot-actions">${actionButtons(row)}</td>' in WAREHOUSE_HTML
    assert "inventoryTimeArchiveHtml" not in WAREHOUSE_HTML
    assert 'title="${h(row.age_warning_text||"")}"' in WAREHOUSE_HTML

    molds = WAREHOUSE_HTML.split("function renderMolds(){", 1)[1].split(
        "function renderMoldPager", 1
    )[0]
    assert 'return `<tr class="compact-ledger-row">' in molds
    assert "assetTimeArchiveHtml(row)" not in molds
    assert "archiveMeta" not in molds
    assert "可人工封存（不会自动执行）" not in molds


def test_finished_lot_editor_requires_explicit_edit_and_keeps_stock_age_derived() -> None:
    editor = WAREHOUSE_HTML.split('<div id="lotEditMask"', 1)[1].split(
        '<div id="assignmentPanel"', 1
    )[0]
    for element_id in (
        "lotEditForm",
        "lotEditOwnership",
        "lotEditCustomer",
        "lotEditProductKeyword",
        "lotEditProductCandidates",
        "lotEditQuantity",
        "lotEditLocation",
        "lotEditStockDate",
    ):
        assert f'id="{element_id}"' in editor
    assert "修改后统一写入库存流水" in editor
    assert "日期不明时不计算精确库龄" in editor
    assert "直接修改库龄" not in editor
    assert 'role="dialog"' in editor
    assert 'aria-modal="true"' in editor
    assert 'aria-labelledby="lotEditTitle"' in editor
    assert 'id="lotEditSave"' in editor

    actions = WAREHOUSE_HTML.split("function actionButtons(row){", 1)[1].split(
        "async function openProductAssignments", 1
    )[0]
    assert 'row.inventory_type==="finished"' in actions
    assert 'state.user.role!=="admin"' in actions
    assert 'onclick="openLotEditor(${row.id})">编辑</button>' in actions
    assert 'id="lotEditGeneralize"' in editor
    assert "/api/warehouse/lots/${lotId}/transfer-to-general" in WAREHOUSE_HTML
    assert actions.index("return detail+label+staging+edit;") < actions.index(">调整</button>")


def test_finished_lot_editor_scopes_product_match_and_saves_transaction_payload() -> None:
    search = WAREHOUSE_HTML.split("async function searchLotEditProductCandidates(){", 1)[1].split(
        "function openLotEditor", 1
    )[0]
    assert "customer_id:customerId" in search
    assert "q:keyword" in search
    assert "/api/warehouse/floor3/product-candidates?" in search
    assert "requestId!==state.lotEdit.requestId" in search
    assert "clearLotEditProductSelection()" in search
    ownership = WAREHOUSE_HTML.split("function updateLotEditOwnership", 1)[1].split(
        "function scheduleLotEditProductSearch", 1
    )[0]
    assert "clearTimeout(state.lotEdit.searchTimer)" in ownership
    assert "state.lotEdit.requestId+=1" in ownership
    assert '$("lotEditProductKeyword").value=""' in ownership
    assert '$("lotEditOwnership").onchange=normalizeLotEditOwnership' in WAREHOUSE_HTML
    assert "请使用下方‘转为通用库存’" in WAREHOUSE_HTML

    save = WAREHOUSE_HTML.split("async function saveLotEditor(event){", 1)[1].split(
        "function updateFlutes", 1
    )[0]
    assert "/api/warehouse/lots/${lotId}/edit-finished" in save
    assert "transfer-to-general" in WAREHOUSE_HTML
    for field in (
        "expected_version",
        "is_general",
        "customer_id",
        "product_id",
        "quantity_available",
        "location_id",
        "stock_date",
        "idempotency_key",
    ):
        assert field in save
    assert "客户专用库存必须选择客户" in save
    assert "请选择与客户匹配的存货编码" in save
    assert "可用数量必须是大于或等于 0 的整数" in save
    assert 'if(await loadLots()===false)throw new Error("库存列表刷新失败")' in save
    assert 'if(state.tab==="locations")await loadFloor3Locations(true)' in save
    assert "event.preventDefault();if(state.lotEdit.saving)return" in save
    assert "state.lotEdit.saving=true" in save
    assert '$("lotEditSave").disabled=true' in save
    assert "sessionId=state.lotEdit.sessionId" in save
    assert "state.lotEdit.sessionId===sessionId&&state.lotEdit.lotId===lotId" in save
    assert 'event.key!=="Escape"' in WAREHOUSE_HTML


def test_floor3_clear_uses_one_confirmation_without_reason_prompt() -> None:
    clear_block = WAREHOUSE_HTML.split("async function clearFloor3Pallet(palletId){", 1)[1].split(
        "async function setFloor3Relocation", 1
    )[0]
    assert clear_block.count("confirm(") == 1
    assert "prompt(" not in clear_block
    assert "正式成品库存未清零时系统会拒绝" in clear_block
    assert "JSON.stringify({expected_version:expectedVersion})" in clear_block


def test_floor3_dynamic_bind_customers_are_redrawn_after_customer_load() -> None:
    loader = WAREHOUSE_HTML.split("async function loadCustomers(){", 1)[1].split(
        "function clearFinishedProductSelection", 1
    )[0]
    assert 'state.customers=data.items' in loader
    assert 'x.customer_code?`${x.customer_code} · ${x.name}`:x.name' in loader
    assert "syncFloor3BindRows();renderFloor3BindRows()" in loader
    assert loader.index("state.customers=data.items") < loader.index("renderFloor3BindRows()")
    assert 'class="floor3-bind-customer" onchange="scheduleFloor3BindCandidateSearch(' in WAREHOUSE_HTML


def test_floor3_bind_candidate_search_is_guarded_debounced_and_stale_safe() -> None:
    schedule = WAREHOUSE_HTML.split("function scheduleFloor3BindCandidateSearch(index){", 1)[1].split(
        "async function searchFloor3BindCandidates", 1
    )[0]
    search = WAREHOUSE_HTML.split("async function searchFloor3BindCandidates(index){", 1)[1].split(
        "function selectFloor3BindCandidate", 1
    )[0]
    sync = WAREHOUSE_HTML.split("function syncFloor3BindRows(){", 1)[1].split(
        "function setFloor3BindCandidateMessage", 1
    )[0]
    assert "setTimeout(()=>searchFloor3BindCandidates(index),260)" in schedule
    assert "if(!row.keyword)" in schedule and "if(!row.keyword)" in search
    assert search.index("if(!row.keyword)") < search.index("api(`/api/warehouse/floor3/product-candidates?")
    assert "requestId=++row.requestId" in search
    assert "requestId!==row.requestId" in search
    assert "limit:30" in search
    assert "criteriaChanged" in sync
    assert "row.searched=false" in sync
    assert "row.candidates=[];row.candidate=null" in sync
    assert "if(criteriaChanged)renderFloor3BindSelected(index)" in sync
    assert 'oninput="scheduleFloor3BindCandidateSearch(' in WAREHOUSE_HTML
    assert 'data-floor3-bind-selected="${index}"' in WAREHOUSE_HTML
    assert "renderFloor3BindCandidateResults(index)" in search
    assert "row.searched=true" in search
    assert "renderFloor3BindRows()" not in search
    assert "没有找到匹配产品，已保留输入内容" in WAREHOUSE_HTML


def test_floor3_bind_panel_lives_in_detail_rail_and_closes_independently() -> None:
    assert WAREHOUSE_HTML.count('id="floor3BindPanel"') == 1
    assert '<div id="floor3ContextPanels">' in WAREHOUSE_HTML
    assert WAREHOUSE_HTML.index('id="floor3ContextPanels"') < WAREHOUSE_HTML.index('id="floor3DetailPanel"')
    assert WAREHOUSE_HTML.index('id="floor3AreaDetailRail"') < WAREHOUSE_HTML.index('id="floor3BindPanel"')
    assert 'id="floor3BindClose"' in WAREHOUSE_HTML
    close_bind = WAREHOUSE_HTML.split("function closeFloor3BindPanel(){", 1)[1].split(
        "function floor3BindRowDefaults", 1
    )[0]
    assert "state.floor3.bindPanelOpen=false" in close_bind
    assert "floor3ReturnBindPanelHome()" in close_bind
    assert "state.floor3.bindPanelOpen=false;" in WAREHOUSE_HTML
    assert 'onclick="openFloor3BindPanel()">绑定货物' in WAREHOUSE_HTML
    assert "<b>当前位空闲</b>" in WAREHOUSE_HTML
    assert 'const relocationAction=!pallet||rack?"":' in WAREHOUSE_HTML
    assert ".floor3-area-focus #floor3AreaDetailRail .floor3-detail-grid{display:block}" in WAREHOUSE_HTML
    assert ".floor3-area-focus #floor3AreaDetailRail .btn{max-width:100%;white-space:normal}" in WAREHOUSE_HTML
    assert ".floor3-area-focus #floor3AreaDetailRail{overflow:visible}" in WAREHOUSE_HTML
    assert ".floor3-area-focus #floor3BindPanel,.floor3-area-focus #floor3AddItemPanel{max-height:none;overflow:visible}" in WAREHOUSE_HTML
    assert 'state.floor3.bindPanelOpen=true;$("floor3DetailPanel").classList.add("hidden")' in WAREHOUSE_HTML
    assert ".floor3-area-focus>div:first-child{min-width:0}" in WAREHOUSE_HTML
    assert ".floor3-area-slot-map{position:relative;width:100%;max-width:100%;min-width:0" in WAREHOUSE_HTML


def test_floor3_overview_empty_slot_can_open_the_shared_bind_panel() -> None:
    overview = WAREHOUSE_HTML.split("function floor3OverviewSelectionHtml(row){", 1)[1].split(
        "function clearFloor3OverviewSelection", 1
    )[0]
    opener = WAREHOUSE_HTML.split("function openFloor3OverviewBindPanel(locationId){", 1)[1].split(
        "function closeFloor3BindPanel", 1
    )[0]
    save = WAREHOUSE_HTML.split("async function saveFloor3Pallet(event){", 1)[1].split(
        "function toggleFloor3AddItemPanel", 1
    )[0]
    assert "当前位空闲" in overview
    assert "openFloor3OverviewBindPanel(${row.id})" in overview
    assert "绑定货物" in overview
    assert "floor3LocationOccupied(row)" in opener
    assert "selection.appendChild(panel)" in opener
    assert "state.floor3.selectedLocationId=Number(locationId)" in opener
    assert "floor3ReturnBindPanelHome()" in WAREHOUSE_HTML
    assert "const locationId=Number(state.floor3.selectedLocationId),layoutVersion=floor3LayoutVersionForLocation(locationId)" in save
    assert 'overview=state.floor3.viewMode==="overview"' in save
    assert "expected_layout_version:layoutVersion" in save
    assert "if(overview)state.floor3.mapPopoverLocationId=locationId" in save
    assert ".floor3-overview-selection #floor3BindPanel{max-height:none" in WAREHOUSE_HTML


def test_floor3_current_goods_labels_wrap_without_nested_scrollbars() -> None:
    for label in ("客户", "存货编码", "订单", "货物类型"):
        assert f"<small>{label}</small>" in WAREHOUSE_HTML
    assert "floor3-item-labels" in WAREHOUSE_HTML
    assert ".floor3-item-label b{display:block;overflow-wrap:anywhere;white-space:normal}" in WAREHOUSE_HTML


def test_floor3_units_are_localized_and_right_rail_has_no_quantity_total() -> None:
    assert 'boxes:"个",sheets:"张"' in WAREHOUSE_HTML
    assert "function floor3UnitLabel(unit){return labels[unit]||unit||\"件\"}" in WAREHOUSE_HTML
    summary = WAREHOUSE_HTML.split("function floor3PalletSummary(row)", 1)[1].split(
        "function floor3MarkerTooltip", 1
    )[0]
    assert "floor3QuantityHtml(item)" in summary
    assert "合计：" not in summary
    assert "floor3UnitLabel(item.unit)" in WAREHOUSE_HTML
    assert "floor3-item-quantity" in WAREHOUSE_HTML


def test_floor3_finished_binding_and_legacy_promotion_use_explicit_contract() -> None:
    build = WAREHOUSE_HTML.split("function floor3BuildItem(row){", 1)[1].split(
        "async function saveFloor3Pallet", 1
    )[0]
    for field in ("create_finished_inventory", "stock_date", "idempotency_key"):
        assert field in build
    assert 'createFinishedInventory=!row.pending&&row.itemType==="finished"' in build
    assert "stock_date:createFinishedInventory?today():null" in build
    assert "idempotency_key:createFinishedInventory?row.idempotencyKey:null" in build
    assert "createFinishedInventory&&!Number.isInteger(quantity)" in build
    save = WAREHOUSE_HTML.split("async function saveFloor3Pallet(event){", 1)[1].split(
        "function toggleFloor3AddItemPanel", 1
    )[0]
    assert "hasFinishedInventory&&hasSnapshot" in save
    assert "正式成品行与现场快照行不能混合" in save
    assert "item?.official_inventory===false" in WAREHOUSE_HTML
    assert "转为正式成品库存" in WAREHOUSE_HTML
    assert '["finished","semi_finished"].includes(item?.item_type)' in WAREHOUSE_HTML
    assert "/api/warehouse/pallets/${palletId}/items/${itemId}/promote-finished" in WAREHOUSE_HTML
    promotion = WAREHOUSE_HTML.split("async function promoteFloor3FinishedItem", 1)[1].split(
        "async function moveFloor3Pallet", 1
    )[0]
    assert "expected_version:Number(expectedVersion)" in promotion
    assert "stock_date:stockDate" in promotion
    assert "confirmed:true" in promotion
    assert "promotionKeys[itemId]" in promotion
    assert "idempotency_key:idempotencyKey" in promotion
    assert "promotionPending" in promotion
    assert promotion.count("confirm(") == 2
    for label in ("客户：", "存货编码：", "产品：", "数量：", "当前位置：", "入库日期："):
        assert label in promotion
    assert "不会直接用盘点快照抵扣" in promotion


def test_floor3_structured_ground_and_temporary_cells_support_pallet_drag() -> None:
    cell = WAREHOUSE_HTML.split("function floor3FRackCell(row,bottom=false){", 1)[1].split(
        "function floor3FRackCard", 1
    )[0]
    assert "floor3CanMovePallet(row)" in cell
    assert "floor3CanReceivePallet(row)" in cell
    assert "floor3StartPalletDrag" in cell
    assert "floor3DropPallet" in cell
    assert "draggable=\"${movable}\"" in cell
    assert "function floor3CanMovePallet(row)" in WAREHOUSE_HTML
    assert "!floor3IsRack(row)" in WAREHOUSE_HTML
    refresh = WAREHOUSE_HTML.split("async function refreshFloor3AfterPalletMove", 1)[1].split(
        "async function confirmFloor3MapMove", 1
    )[0]
    assert "closeFloor3Detail();await refreshFloor3LocationData()" in refresh
    assert "await openFloor3Location(targetId)" in refresh


def test_semi_finished_location_dropdown_uses_active_placed_locations_and_explains_empty_state() -> None:
    loader = WAREHOUSE_HTML.split("async function loadLocations(includeInactive=false){", 1)[1].split(
        "async function loadCustomers", 1
    )[0]
    assert 'const semiLocations=active.filter(x=>x.position_status==="mapped"&&["semi_finished","shared"].includes(x.warehouse_type))' in loader
    assert '$("siLocation").disabled=!semiLocations.length' in loader
    assert 'id="siLocationHint"' in WAREHOUSE_HTML
    assert "暂无半成品库位，请先完成 SF-TEMP 迁移或新增半成品库位" in WAREHOUSE_HTML

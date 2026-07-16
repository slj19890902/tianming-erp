from pathlib import Path
import json
import re
import subprocess


WAREHOUSE_HTML = (Path(__file__).resolve().parents[1] / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_location_management_is_single_entry_with_floor3_and_ledger_views() -> None:
    assert WAREHOUSE_HTML.count('data-tab="locations"') == 1
    assert 'data-tab="floor3_locations"' not in WAREHOUSE_HTML
    assert 'data-location-view="floor3"' in WAREHOUSE_HTML
    assert 'data-location-view="ledger"' in WAREHOUSE_HTML
    assert 'data-location-view="ledger" type="button">全部库位台账' in WAREHOUSE_HTML
    assert 'async function switchLocationView(view)' in WAREHOUSE_HTML
    assert 'function canManageLocations(){return state.user?.role==="admin"}' in WAREHOUSE_HTML
    assert 'if(view==="ledger"&&!canManageLocations())' in WAREHOUSE_HTML
    assert 'if(tab==="locations"){\n        state.locationView="floor3"' in WAREHOUSE_HTML
    assert 'state.tab==="locations"&&state.locationView==="floor3"' in WAREHOUSE_HTML
    assert 'id="floor3LocationSection"' in WAREHOUSE_HTML
    assert 'id="locationSection"' in WAREHOUSE_HTML
    assert 'id="floor3AreaFilter"' in WAREHOUSE_HTML
    assert 'id="floor3OccupancyFilter"' in WAREHOUSE_HTML
    assert 'id="floor3KeywordFilter"' in WAREHOUSE_HTML
    assert 'page_size:"500"' in WAREHOUSE_HTML
    assert 'FLOOR3_AREA_OPTIONS=[' in WAREHOUSE_HTML
    for area_code in ("A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2", "CD1", "D1", "D2", "DE1", "E1", "E2", "E3", "E4", "F1", "F2", "F3", "F4"):
        assert f'["{area_code}"' in WAREHOUSE_HTML
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
    assert "state.floor3.mapLocations=overview.items||[]" in refresh
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
    assert "location_id:Number(state.floor3.selectedLocationId)" in WAREHOUSE_HTML
    assert "pallet_code:" in WAREHOUSE_HTML
    assert "items})" in WAREHOUSE_HTML
    assert 'body:JSON.stringify({expected_version:expectedVersion,item})' in WAREHOUSE_HTML
    assert "function floor3ExpectedVersion(palletId)" in WAREHOUSE_HTML
    assert "expected_version:expectedVersion" in WAREHOUSE_HTML
    assert "to_location_id:Number(targetId)" in WAREHOUSE_HTML
    assert "expected_version:expectedVersion,remarks" in WAREHOUSE_HTML
    assert "confirmed:true" in WAREHOUSE_HTML
    assert "idempotency_key:createIdempotencyKey()" in WAREHOUSE_HTML
    assert "needs_relocation:Boolean(needsRelocation)" in WAREHOUSE_HTML
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
    assert "当前货位已占用（客户范围受限）" in WAREHOUSE_HTML
    assert "位置内容受客户范围权限保护" in WAREHOUSE_HTML
    assert "pallet&&!restricted&&canOperate()?" in detail_block
    assert "pallet&&canOperate()?" not in detail_block
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


def test_floor3_is_mobile_card_layout_and_has_unlimited_rows() -> None:
    assert "floor3-location-grid" in WAREHOUSE_HTML
    assert "@media(max-width:560px)" in WAREHOUSE_HTML
    assert "floor3-toolbar>*{width:100%!important" in WAREHOUSE_HTML
    assert "添加同栈板产品（至少可加到5行）" in WAREHOUSE_HTML
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
    for label in ("客户", "存货编码", "产品名称", "数量", "款式数"):
        assert label in summary_block


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
    assert 'panel.innerHTML=area==="F"?floor3FSecondaryHtml(rows)' in WAREHOUSE_HTML
    assert "F 区概览" not in WAREHOUSE_HTML
    assert ".floor3-area-focus.floor3-f-mode{grid-template-columns:repeat(2,minmax(0,1fr))}" in WAREHOUSE_HTML
    assert 'classList.toggle("floor3-f-mode",areaCode==="F")' in WAREHOUSE_HTML


def test_floor3_d1_has_two_ground_pallet_rows_and_one_second_level_rack_row() -> None:
    assert 'if(areaCode==="D1")return 28' in WAREHOUSE_HTML
    assert "function floor3D1DefaultDisplayLayout(row)" in WAREHOUSE_HTML
    assert '/^D1-S2-(\\d{2})$/' in WAREHOUSE_HTML
    assert '/^D1-([LR])(\\d{2})$/' in WAREHOUSE_HTML
    assert "function floor3D1LayoutHtml(rows)" in WAREHOUSE_HTML
    assert "二层货架格（8 格）" in WAREHOUSE_HTML
    assert "底部栈板位（两排，各 10 位）" in WAREHOUSE_HTML
    assert 'area==="D1"?floor3D1LayoutHtml(rows)' in WAREHOUSE_HTML


def test_floor3_e4_and_de1_follow_vertical_physical_order() -> None:
    assert "function floor3E4DefaultDisplayLayout(row)" in WAREHOUSE_HTML
    assert "function floor3DE1DefaultDisplayLayout(row)" in WAREHOUSE_HTML
    assert "function floor3VerticalStructureHtml(areaCode,rows)" in WAREHOUSE_HTML
    assert "一列货架 / 两列栈板" in WAREHOUSE_HTML
    assert "左侧货架（从上到下 4 格）" in WAREHOUSE_HTML
    assert "右侧栈板 L 列（6 位）" in WAREHOUSE_HTML
    assert "右侧栈板 R 列（6 位）" in WAREHOUSE_HTML
    assert "栈板位（从上到下）" in WAREHOUSE_HTML
    assert 'area==="E4"||area==="DE1"' in WAREHOUSE_HTML
    assert ".floor3-slot-marker.area-e4,.floor3-slot-marker.area-de1" in WAREHOUSE_HTML


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
    assert '$("floor3AreaFilter").onchange=event=>selectFloor3Area(event.target.value)' in WAREHOUSE_HTML
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
    assert 'if(!canOperate()||!state.floor3.moveMode){toast("请先进入库位调整模式",true);return}' in WAREHOUSE_HTML
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
    assert "body.floor3-mode header,body.floor3-mode .wrap>.tabs,body.floor3-mode #locationViewTabs{display:none}" in WAREHOUSE_HTML
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
    assert len([code for code in re.findall(r'id:"([A-Z0-9]+)"', zones) if code not in f_area_codes]) == 16

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
    assert "当前货位已占用（客户范围受限）" in WAREHOUSE_HTML
    assert "<b>占用</b>" in WAREHOUSE_HTML
    summary = WAREHOUSE_HTML.split("function floor3PalletSummary(row)", 1)[1].split(
        "function floor3SlotMarker", 1
    )[0]
    assert "items.reduce((sum,item)=>sum+(Number(item.quantity)||0),0)" in summary
    assert "item.inventory_code||item.product_code" in summary
    assert "<b>合计：${h(total)}</b>" in summary
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
    assert '"地图拖拽移位"' in WAREHOUSE_HTML
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
    assert "function floor3CanEditLayout(){return state.user?.role===\"admin\"}" in WAREHOUSE_HTML
    assert "function floor3StartLayoutDrag" in WAREHOUSE_HTML
    assert "function floor3SetAreaBackdrop(areaCode)" in WAREHOUSE_HTML
    assert "target.dataset.areaCode=zone.id" in WAREHOUSE_HTML
    assert "floor3AreaBackdropHtml" in WAREHOUSE_HTML
    assert "当前登记数量合计" in WAREHOUSE_HTML
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


def test_floor3_e4_overview_moves_only_rack_half_a_cell_left() -> None:
    assert '.floor3-slot-marker.overview-e4-rack{transform:translateX(-50%)}' in WAREHOUSE_HTML
    marker_block = WAREHOUSE_HTML.split("function floor3SlotMarker(row,areaFocus=false){", 1)[1].split(
        "function floor3RackLevel", 1
    )[0]
    assert '!areaFocus&&floor3AreaCode(row)==="E4"&&floor3IsRack(row)?"overview-e4-rack":""' in marker_block
    assert 'left_pct:ground[1]==="L"?58:128' in WAREHOUSE_HTML
    assert 'width_pct:63,height_pct:13.5' in WAREHOUSE_HTML


def test_floor3_clear_requires_two_confirmations_and_move_filters_empty_active() -> None:
    assert WAREHOUSE_HTML.count('confirm("确认清空当前栈板？') == 1
    assert WAREHOUSE_HTML.count('confirm("请再次确认：清空后') == 1
    assert "floor3CanReceivePallet(row)" in WAREHOUSE_HTML


def test_floor3_move_targets_are_loaded_independently_from_browse_filters() -> None:
    loader = WAREHOUSE_HTML.split("async function loadFloor3MoveLocations(){", 1)[1].split(
        "function renderFloor3Locations(){", 1
    )[0]
    assert 'queryString({occupancy:"empty",page_size:"500"})' in loader
    assert '$("floor3AreaFilter")' not in loader
    assert '$("floor3KeywordFilter")' not in loader
    assert '$("floor3OccupancyFilter")' not in loader
    assert "filter(floor3CanReceivePallet)" in loader
    assert "state.floor3.moveLocations.filter" in WAREHOUSE_HTML
    assert "state.floor3.locations.filter(row=>Number(row.id)!==Number(currentId)" not in WAREHOUSE_HTML


def test_finished_in_is_one_step_and_refreshes_inventory_and_floor3_map() -> None:
    assert '<button id="openInForm" class="btn primary operate-only">入成品仓</button>' in WAREHOUSE_HTML
    assert 'tab==="finished"?"入成品仓":"入半成品仓"' in WAREHOUSE_HTML
    assert "入成品仓（一步完成）" in WAREHOUSE_HTML
    assert "做完马上送货时无需办理入仓" in WAREHOUSE_HTML
    assert "只选客户、产品、数量和三楼货位" in WAREHOUSE_HTML
    assert "可选：修改日期、来源或备注" in WAREHOUSE_HTML
    assert 'source_version==="V11"?`三楼 ${x.area_code||""}`:"其他库位"' in WAREHOUSE_HTML
    save_block = WAREHOUSE_HTML.split("async function saveFinished(e){", 1)[1].split(
        "async function saveSemi", 1
    )[0]
    assert "/api/warehouse/finished/manual-in" in save_block
    assert "await loadLots();await loadFloor3Locations()" in save_block
    assert "已入成品仓并绑定" in save_block
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
    assert "fgProductSelected" in WAREHOUSE_HTML

    save = WAREHOUSE_HTML.split("async function saveFinished(e){", 1)[1].split(
        "async function saveSemi", 1
    )[0]
    assert "if(!productId)" in save
    assert "点击选择匹配候选" in save
    assert 'product_id:productId' in save


def test_floor3_clear_cancel_and_blank_reason_do_not_send_request() -> None:
    clear_block = WAREHOUSE_HTML.split("async function clearFloor3Pallet(palletId){", 1)[1].split(
        "async function setFloor3Relocation", 1
    )[0]
    assert 'const response=prompt("请输入清空备注")' in clear_block
    assert "if(response===null)return" in clear_block
    assert "const remarks=response.trim()" in clear_block
    assert "if(!remarks){toast(" in clear_block
    assert "return}" in clear_block
    assert '||"现场清空"' not in clear_block
    assert clear_block.index("if(response===null)return") < clear_block.index("/clear`")
    assert clear_block.index("if(!remarks){toast(") < clear_block.index("/clear`")

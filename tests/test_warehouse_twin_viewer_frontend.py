from __future__ import annotations

import json
from pathlib import Path

from app.services.warehouse_twin_layout import load_warehouse_twin_floor


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx").read_text(encoding="utf-8")
MOVE_DRAFT = (ROOT / "factory_twin" / "frontend" / "src" / "warehouseMoveDraft.mjs").read_text(encoding="utf-8")
STOCKTAKE_DRAFT = (ROOT / "factory_twin" / "frontend" / "src" / "warehouseStocktakeDraft.mjs").read_text(encoding="utf-8")
CANVAS = (ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx").read_text(encoding="utf-8")
INDUSTRIAL = (ROOT / "factory_twin" / "frontend" / "src" / "industrialScene.ts").read_text(encoding="utf-8")
BUILT = (ROOT / "static" / "factory-twin-assets" / "warehouse-twin.html").read_text(encoding="utf-8")
TWIN_CSS = (ROOT / "factory_twin" / "frontend" / "src" / "warehouseTwin.css").read_text(encoding="utf-8")
ERP_INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
TWIN_LAYOUT = json.loads((ROOT / "static" / "factory_maps" / "twin_layout_v1.json").read_text(encoding="utf-8"))


def test_operational_twin_reuses_the_editor_renderer_for_2d_and_25d() -> None:
    assert 'import { EditorCanvas, type CanvasFocusTarget } from "./EditorCanvas"' in SOURCE
    assert 'visualTheme="warehouse"' in SOURCE
    assert "readOnly" in SOURCE
    assert 'setViewMode("2d")' in SOURCE
    assert 'setViewMode("25d")' in SOURCE
    assert "二维平面" in SOURCE
    assert "等距视图" in SOURCE
    assert "2.5D 等距" not in SOURCE
    assert 'visualTheme?: "editor" | "warehouse"' in CANVAS


def test_operational_twin_reuses_formal_inventory_and_does_not_fake_rack_positions() -> None:
    assert 'days: "30"' in SOURCE
    assert "/api/warehouse/twin-dashboard/overview?${params.toString()}" in SOURCE
    assert "/api/warehouse/twin-operations/locate?${params.toString()}" in SOURCE
    assert "/api/warehouse/molds/by-map-area?${params.toString()}" in SOURCE
    assert "当前区域模具筛选" in SOURCE
    assert 'selectedAreaIsMold ? `${moldAreaResponse?.total || 0} 件`' in SOURCE
    assert "未填写位置或仍使用旧自由文本位置的模具" in SOURCE
    assert "库存只投影到已确认区域，不虚构货架层、格或箱体坐标" not in SOURCE
    assert "暂无已建空货位" in SOURCE
    # P1-49C reuses the formal pallet merge capability through one batch endpoint;
    # the selected pallets still come from real ERP locations, not fake map positions.
    assert '"/api/warehouse/pallets/merge-batches"' in SOURCE
    assert "交换平面位置不改变库存" in SOURCE or "交换二维平面位置；库存和栈板绑定未改变" in SOURCE
    assert "/api/warehouse/twin-production/layouts/" in SOURCE
    assert "只保存隔离地图中的任务与位置关系" in SOURCE
    assert "生产任务、数量和状态未修改" in SOURCE


def test_mold_rack_planning_and_lookup_use_the_live_mold_ledger() -> None:
    assert "/api/warehouse/molds/by-map-rack?${params.toString()}" in SOURCE
    assert "实时模具货架" in SOURCE
    assert "LIVE MOLD ASSET ELEVATION" not in SOURCE
    assert "同一格可登记多件模具" in SOURCE
    assert "关联产品</dt>" in SOURCE
    assert "直接选择货架，设置层数和每层格数" in SOURCE
    assert "保存层格到草稿" in SOURCE
    assert "发布成功后失效位置统一归入首个可用格并写移动流水" in SOURCE
    assert 'min="1" max="20" value={selectedRackEditDraft.levels}' in SOURCE
    assert "mold_location_reassignment_count" in SOURCE
    assert "!advancedAreaMaintenanceOpen && !rack?.mold_rack_code" in SOURCE
    assert "moldRacksForArea(selectedAreaFeature" in SOURCE
    assert "打开模具货架正视图" in SOURCE
    assert "当前产品书脊" in SOURCE
    assert "点击存货编码或名称，右侧查看产品、模具和正式位置" in SOURCE
    assert ".mold-rack-book-spines" in TWIN_CSS
    assert ".twin-mold-rack-stage" in TWIN_CSS
    assert ".twin-mold-rack-planner" in TWIN_CSS


def test_operational_twin_uses_erp_session_for_real_production_and_manual_mapping() -> None:
    assert 'credentials: "same-origin"' in SOURCE
    assert "只读定位，不改数量和状态" in SOURCE
    assert "当前没有待生产任务。" in SOURCE
    assert "确认投影到地图" in SOURCE
    assert "productionProjections={productionProjection?.items || EMPTY_PRODUCTION_PROJECTIONS}" in SOURCE
    assert "raw.pallets || []" in SOURCE
    assert "pallets: groundCandidatePallets" in SOURCE
    assert "pallets: [...layout.pallets, ...movePreviewPallets]" not in SOURCE


def test_operational_twin_declutters_labels_and_keeps_details_in_the_inspector() -> None:
    assert "labels: false" in SOURCE
    assert 'query.get("embedded") === "1"' in SOURCE
    assert '<nav className="twin-floor-switch" aria-label="楼层切换">' in SOURCE
    assert "filterOperationalFeatures(raw.floor_code, raw.bounds_mm" in SOURCE
    assert 'onSelect={selectOperationalEntity}' in SOURCE
    assert "库存与库位" not in SOURCE
    assert '<aside className="twin-inspector">' in SOURCE
    assert 'className="twin-location-card"' in SOURCE
    assert 'if (layers.labels) {' in CANVAS
    assert "warehouseFrustumDivisor(layout.floor_code, visualTheme)" in CANVAS
    assert "aisleSurfaceStyle(visualTheme)" in CANVAS


def test_phase2c15_uses_low_cost_warehouse_rendering_on_factory_computers() -> None:
    assert 'query.get("view") === "25d" ? "25d" : "2d"' in SOURCE
    assert 'view=2d' in ERP_INDEX
    assert "new THREE.WebGLRenderer({ antialias: !warehouseTheme" in CANVAS
    assert "warehouseTheme ? 1 : 2" in CANVAS
    assert "addWarehousePalletInstances(scene, warehousePalletInstances, viewMode)" in CANVAS
    assert "new THREE.InstancedMesh" in CANVAS
    assert "export function palletMarkerSpec" in INDUSTRIAL
    assert "proxy.layers.set(WAREHOUSE_PICK_LAYER)" in CANVAS
    assert "raycaster.layers.set(warehouseTheme ? WAREHOUSE_PICK_LAYER : 0)" in CANVAS
    assert "warehousePickProxy(group)" in CANVAS
    assert "intersectObjects(interactive, !warehouseTheme)" in CANVAS
    assert "let pointerMoveFrame: number | null = null" in CANVAS
    assert "latestPointerMove" in CANVAS
    assert "const requestRender = () =>" in CANVAS
    assert "requestAnimationFrame(animate)" not in CANVAS
    assert "syncResultHighlights(runtime, highlightFeatureIds, highlightedPalletIds)" in CANVAS
    canvas_effect_dependencies = CANVAS.split("}, [layout, assets, viewMode, cameraPreset, viewResetToken, layers", 1)[1].split("]);", 1)[0]
    assert "highlightFeatureIds" not in canvas_effect_dependencies
    assert "highlightedPalletIds" not in canvas_effect_dependencies


def test_operational_twin_uses_tianming_erp_compact_shell() -> None:
    assert '<div><h1>仓库地图</h1></div>' in SOURCE
    assert "数字孪生智慧仓储" not in SOURCE
    assert 'const [layerPanelOpen, setLayerPanelOpen] = useState(false)' in SOURCE
    assert 'const [productionPanelOpen, setProductionPanelOpen] = useState(false)' in SOURCE
    assert 'aria-expanded={layerPanelOpen}' in SOURCE
    assert 'aria-expanded={productionPanelOpen}' in SOURCE
    assert 'className={`twin-workspace ${layerPanelOpen ? "layers-open" : "layers-collapsed"}' in SOURCE
    assert 'searchPanelOpen ? "context-open" : "context-collapsed"' in SOURCE
    assert "默认先看真实三楼地图；切换其它模块不会丢失当前仓库页面。" not in ERP_INDEX
    assert "warehouse-floor-card" not in ERP_INDEX
    assert "height:100%" in ERP_INDEX
    assert "'warehouse-main': activePage === 'warehouse'" in ERP_INDEX
    assert ".main.warehouse-main" in ERP_INDEX
    assert "--twin-bg: #f3f5f7" in TWIN_CSS
    assert "overflow-x: hidden" in TWIN_CSS


def test_embedded_warehouse_shell_has_a_definite_visible_height() -> None:
    assert (
        ".warehouse-shell {\n"
        "        grid-row:1; height:100%; min-height:0; overflow:hidden;\n"
        "        display:flex; flex-direction:column; gap:0;\n"
        "      }"
    ) in ERP_INDEX
    assert (
        ".warehouse-shell-frame {\n"
        "        display:block; width:100%; height:auto; min-height:0; flex:1 1 auto;"
    ) in ERP_INDEX


def test_embedded_twin_opens_the_ledger_in_the_top_level_page() -> None:
    assert '<a className="twin-ledger-link" href="/warehouse-ledger.html?tab=finished" target="_top">库存台账</a>' in SOURCE
    assert 'className={`warehouse-twin-shell ${embedded ? "embedded-shell" : ""}' in SOURCE
    assert ".warehouse-twin-shell.embedded-shell .twin-command-bar" in TWIN_CSS
    assert "display: none" in TWIN_CSS


def test_operational_twin_keeps_fixed_objects_locked_and_only_adds_location_pallet_interaction() -> None:
    assert 'entity.kind === "equipment"' in SOURCE
    assert 'item.feature_kind === "zone"' in SOURCE
    assert 'setSelected({ kind: task.mapping.target_kind === "pallet" ? "pallet"' not in SOURCE
    assert '{selectedRack && <section className="twin-object-card">' not in SOURCE
    assert '{selectedStructure && <section className="twin-object-card">' not in SOURCE
    assert 'operationalEntitySelectable(visualTheme, "feature", feature.feature_kind)' in CANVAS
    assert 'operationalEntitySelectable(visualTheme, "equipment")' in CANVAS
    assert 'operationalEntitySelectable(visualTheme, "structure")' in CANVAS


def test_operational_twin_uses_translucent_non_occluding_walls() -> None:
    assert "wallSurfaceStyle(visualTheme, viewMode)" in CANVAS
    assert "transparent: structureWallStyle.transparent" in CANVAS
    assert "opacity: structureWallStyle.opacity" in CANVAS
    assert "depthWrite: structureWallStyle.depthWrite" in CANVAS
    assert "transparent: customWallStyle?.transparent" in CANVAS


def test_operational_twin_expands_real_area_inventory_with_local_filter_and_search_focus() -> None:
    assert 'from "./warehouseInventory.mjs"' in SOURCE
    assert "expandAreaInventory(dashboard?.locations || [], floorCode, selectedAreaCode)" in SOURCE
    assert "filterAreaInventory(selectedInventory, areaInventorySearch)" in SOURCE
    assert "当前区域库存筛选" in SOURCE
    assert "存货编码、产品、客户、位置" in SOURCE
    assert "全部真实位置已选中" in SOURCE
    assert 'const selectedAreaActivationLabel = selectedAreaHasPublishedBinding' in SOURCE
    assert "区域已启用，当前没有货物" in SOURCE
    assert "库存为 0 不代表区域未启用" in SOURCE
    assert "{formatNumber(inventoryPhysicalQuantity(item))} {inventoryUnitLabel(item.unit)}" in SOURCE
    assert "已预占 {formatNumber(item.reserved_quantity)} {inventoryUnitLabel(item.unit)}" in SOURCE
    assert "visibleSelectedInventory" in SOURCE
    assert "查看全部 ${filteredSelectedInventory.length} 条库存" in SOURCE
    assert "批次与预占详情" in SOURCE


def test_capacity_review_link_keeps_the_clicked_map_area_identity() -> None:
    assert "formal_area_id?: number | null" in SOURCE
    assert (
        "locationEditMode && canEditLocations && selectedAreaHasPublishedBinding "
        "&& selectedAreaFeature.capacity_review_status === 'pending'"
    ) in SOURCE
    assert 'location_view: "ledger"' in SOURCE
    assert 'capacity_review: "1"' in SOURCE
    assert 'area_id: String(selectedAreaFeature.formal_area_id)' in SOURCE
    assert 'floor_id: String(selectedAreaFeature.formal_floor_id || "")' in SOURCE
    assert 'area_code: selectedAreaFeature.erp_area_code' in SOURCE
    assert 'map_feature_id: selectedAreaFeature.id' in SOURCE
    assert 'href={selectedAreaCapacityReviewUrl}' in SOURCE
    assert "已启用区域可直接在下方填写最大栈板数并一次确认" in SOURCE
    assert "单独复核容量" in SOURCE
    assert '/warehouse-ledger.html?location_view=ledger&capacity_review=1"' not in SOURCE


def test_unbound_measured_zone_does_not_offer_a_disconnected_capacity_review() -> None:
    assert "尚未绑定正式区域" in SOURCE
    assert "直接使用下方简化表单确认用途、形式和容量" in SOURCE
    assert "选用现有未绑定区域" in SOURCE
    assert "确认并启用此区域" in SOURCE
    assert "selectedAreaFeature.formal_binding_status === 'draft'" in SOURCE
    assert "区域绑定草稿待处理" in SOURCE


def test_floor1_candidate_blockers_offer_direct_actions_and_recheck() -> None:
    assert 'blocking_items: Floor1CandidateBlockingItem[]' in SOURCE
    assert 'floor1CandidateBlockerHref(item)' in SOURCE
    assert 'floor1CandidateBlockerDetail(item)' in SOURCE
    assert 'target="_blank" rel="noreferrer"' in SOURCE
    assert "去移动库存和栈板" in (
        ROOT / "app" / "services" / "warehouse_floor1_candidate_planner.py"
    ).read_text(encoding="utf-8")
    assert "处理完成，重新检查" in SOURCE
    assert 'query.get("mode")' in SOURCE
    assert 'query.get("area_code")' in SOURCE
    assert 'query.get("map_feature_id")' in SOURCE
    assert 'query.get("edit") === "area_policy"' in SOURCE


def test_existing_area_picker_keeps_confirmed_a2_and_rejects_unsafe_candidates() -> None:
    helper = SOURCE[SOURCE.index("export function availableFormalAreasForFeature"):SOURCE.index("async function requestJson")]
    assert "!area.storage_policy" in helper
    assert "Number(area.recorded_location_count || 0) === 0" in helper
    assert "!occupiedDraftCodes.has(area.area_code.toUpperCase())" in helper
    assert "construction_status" not in helper
    assert "capacity_review_status" not in helper
    assert 'existing_area_id: existingAreaId' in SOURCE
    assert 'selectedAreaFeature.formal_area_id && !selectedAreaFeature.formal_policy_status' in SOURCE
    assert 'disabled={Boolean(selectedExistingAreaId)}' in SOURCE
    assert "!selectedAreaHasPublishedBinding" in SOURCE


def test_existing_area_picker_empty_layout_dependencies_are_stable() -> None:
    assert 'import {\n  clearFormalAreaOptions,\n  formalAreaOptionsEffectEnabled,\n  stableTwinFeatures\n} from "./formalAreaOptions.mjs"' in SOURCE
    assert "const features = stableTwinFeatures(layout) as TwinFeature[]" in SOURCE
    assert "setFormalAreaOptions(clearFormalAreaOptions)" in SOURCE
    assert "const shouldLoadFormalAreaOptions = formalAreaOptionsEffectEnabled" in SOURCE
    assert "features = (layout?.features || [])" not in SOURCE
    assert "setFormalAreaOptions([])" not in SOURCE


def test_published_policy_remains_operational_while_an_unrelated_layout_draft_exists() -> None:
    assert 'formal_policy_status?: "draft" | "published" | null' in SOURCE
    assert 'selectedAreaFeature?.formal_policy_status === "published"' in SOURCE
    assert (
        "selectedAreaFeature.formal_binding_status === 'draft' && "
        "selectedAreaFeature.formal_policy_status !== 'published'"
    ) in SOURCE


def test_operational_twin_uses_cross_floor_search_highlights_and_mapped_location_pallets() -> None:
    assert "全仓查找" in SOURCE
    assert "库存编码定位" not in SOURCE
    assert "统一查货" in SOURCE
    assert 'const [searchPanelOpen, setSearchPanelOpen] = useState(true)' in SOURCE
    assert "searchHighlightAreaCodes(searchHighlightItems, floorCode)" in SOURCE
    assert "highlightFeatureIds={searchHighlightFeatureIds}" in SOURCE
    assert "highlightedPalletIds={mapHighlightPalletIds}" in SOURCE
    assert "mergeHighlightPalletIds" in SOURCE
    assert "buildMappedLocationPallets(" in SOURCE
    assert 'item.position_status === "mapped"' in SOURCE
    assert "unlocated_inventory" in SOURCE
    assert "const mappedLocationPallets = useMemo(" in SOURCE
    assert "const movePreviewPallets = useMemo(() =>" in SOURCE
    assert "if (mapMode !== \"move\" || !moveDrafts.length) return mappedLocationPallets" in SOURCE
    assert "pallets: groundCandidatePallets" in SOURCE


def test_area_planning_defaults_to_one_result_oriented_confirmation() -> None:
    assert "主要用来堆放" in SOURCE
    assert "区域形式" in SOURCE
    assert "最大可放栈板数" in SOURCE
    assert "确认并启用此区域" in SOURCE
    assert "系统自动完成保存、校验和启用；不会移动库存、栈板或产品" in SOURCE
    assert "/confirm-area`" in SOURCE
    assert 'primary_inventory_type: simpleAreaUsage' in SOURCE
    assert 'storage_layout: simpleAreaLayout' in SOURCE
    assert 'max_pallet_capacity: capacity' in SOURCE
    assert 'setPlanningPublishedRevision(result.published_revision)' in SOURCE
    assert "原有高级维护草稿已保留，没有随本次确认发布" in SOURCE
    assert 'palletEditingOnly={(locationEditMode && (advancedAreaMaintenanceOpen || Boolean(locationPointEditAreaCode))) || warehouseMoveModeActive}' in SOURCE
    assert "拖动并保存现场货位" in SOURCE
    assert "系统不再强制把栈板紧贴均匀排布" in SOURCE
    assert "主动留出通行、货物外伸和操作间距" in SOURCE
    assert "逻辑货位点（非实尺度）" in SOURCE
    assert 'rackEditingEnabled={locationEditMode && advancedAreaMaintenanceOpen}' in SOURCE
    assert '区域规划 · 一次确认' in SOURCE
    assert "advancedAreaMaintenanceOpen && <div className=\"twin-layout-draft-workflow\"" in SOURCE
    assert "高级维护" in SOURCE
    assert '<aside className="twin-context-rail">' in SOURCE
    assert "库存与库位" not in SOURCE
    assert '<aside className="twin-inspector">' in SOURCE
    assert "twin-stage-footer" not in SOURCE
    assert "twin-system-footer" not in SOURCE


def test_area_confirmation_keeps_the_current_planning_revision_for_the_next_zone() -> None:
    assert "const refreshPlanningTwinFloor = async () =>" in SOURCE
    assert "/api/warehouse/twin-layout/floors/${floorCode}/draft`" in SOURCE
    assert "setLayoutDraftControl(raw.draft_control)" in SOURCE
    assert "setPlanningPublishedRevision(raw.draft_control.published_revision)" in SOURCE
    assert "await Promise.all([refreshPlanningTwinFloor(), refreshDashboard()])" in SOURCE


def test_stale_draft_has_an_explicit_recovery_without_mutating_inventory() -> None:
    assert 'message.includes("当前草稿已过期")' in SOURCE
    assert "放弃旧草稿并重新规划" in SOURCE
    assert "/draft/rebuild-stale`" in SOURCE
    assert "expected_published_revision: publishedFloorRevision" in SOURCE
    assert "正式地图、库存、栈板和产品都不会改变" in SOURCE


def test_same_inventory_and_specification_merge_is_only_a_confirmed_draft() -> None:
    assert "可合并货位" in SOURCE
    assert "同存货编码、同规格可合并建议" not in SOURCE
    assert "useMergeSuggestion" in SOURCE
    assert "加入合并草稿" in SOURCE
    assert "请明确选择一块主栈板后再一次确认" in SOURCE
    assert '"/api/warehouse/pallets/merge-batches"' in SOURCE


def test_move_panel_uses_business_wording_and_keeps_delivery_linkage_clear() -> None:
    assert "已选货物 ·" in SOURCE
    assert "重新选择货物" in SOURCE
    assert "库存、入库来源和送货单均未改变" in SOURCE
    assert "请选择可用空货位" in SOURCE
    assert "不会改变订单和后续送货关系" in SOURCE
    assert "取消来源" not in SOURCE
    assert "当前没有同时满足“候选接口＋已发布地图”的空货位" not in SOURCE


def test_p1_47a_uses_typed_map_search_and_one_unified_read_only_entry() -> None:
    assert 'type WarehouseSearchType = "finished" | "mold" | "printing_plate"' in SOURCE
    assert "纸箱成品" in SOURCE
    assert "模具编码或名称" in SOURCE
    assert "印刷版编码、产品或位置" in SOURCE
    assert "客户、简写、存货编码、产品名称或规格" in SOURCE
    assert "不必先记住存货编码" in SOURCE
    assert "groupSearchProducts(searchResponse?.items || [])" in SOURCE
    assert "全部真实位置已选中" in SOURCE
    assert 'type WarehouseMapMode = "lookup" | "move" | "planning"' in SOURCE
    assert 'const [mapMode, setMapMode] = useState<WarehouseMapMode>(() =>' in SOURCE
    assert 'requested === "move" || requested === "planning"' in SOURCE
    assert "查货模式 · 只读" in SOURCE
    assert "P1-47C 独立阶段启用" not in SOURCE
    assert 'setCanExecuteWarehouse(value.permissions.includes("warehouse.execute"))' in SOURCE
    assert '(canExecuteWarehouse || canStocktake) && <button type="button" className={mapMode === "move" ? "active" : ""} disabled={spatialEditBusy} onClick={enterWarehouseMoveMode}' in SOURCE
    assert "区域规划" in SOURCE
    assert "P1-47B 独立阶段启用" not in SOURCE
    assert 'mapMode === "move"' in SOURCE
    assert 'mapMode === "planning"' in SOURCE
    assert "warehouseSearchFloorSummaries(current.items)" in SOURCE
    assert "warehouseSearchLocationSummaries(current.items)" in SOURCE
    assert "真实文字位置" in SOURCE
    assert "warehouse-search-hit" in SOURCE
    assert "product-search-hit" in SOURCE
    assert "正式货位盘点调整" in SOURCE
    assert "筛选该客户已有产品" in SOURCE
    assert "底部一次确认整批提交" in SOURCE
    assert "buildStocktakeBatchPayload(stocktakeBatchIdempotencyKey, stocktakeDrafts)" in SOURCE
    assert '"/api/warehouse/twin-operations/stocktake-batches"' in SOURCE
    assert "selectedLocation.location_name" in SOURCE
    assert "内部库位编码只在详情中保留" not in SOURCE
    assert "内部码 {item.location_code || \"未编\"}" not in SOURCE
    assert ".twin-location-item.warehouse-search-hit" in TWIN_CSS
    assert ".twin-area-lot.product-search-hit" in TWIN_CSS


def test_phase2c8_keeps_location_layout_editing_in_2d_and_25d_read_only() -> None:
    assert "库位布局" in SOURCE
    assert "二维编辑" in SOURCE
    assert "等距视图仅查看库位与货物标签" in SOURCE
    assert "locationLayoutGeometry(" in SOURCE
    assert "/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(areaCode)}" in SOURCE
    assert "/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(selectedAreaCode)}/location-count" in SOURCE
    assert "/management" in SOURCE
    assert "available_actions" in SOURCE
    assert "系统按区域自动生成内部唯一编码" in SOURCE
    assert "/api/warehouse/spatial-layout/locations/${selectedLocation.location_id}/disable" in SOURCE
    assert "palletEditingOnly={(locationEditMode && (advancedAreaMaintenanceOpen || Boolean(locationPointEditAreaCode))) || warehouseMoveModeActive}" in SOURCE
    assert 'readOnly={(!locationEditMode || (!advancedAreaMaintenanceOpen && !locationPointEditAreaCode)) && !warehouseMoveModeActive}' in SOURCE
    assert "rackEditingEnabled={locationEditMode && advancedAreaMaintenanceOpen}" in SOURCE
    assert "featureEditingEnabled={locationEditMode && advancedAreaMaintenanceOpen && areaPolicyEditMode}" in SOURCE
    assert "选择区域或设备" not in SOURCE


def test_phase2c13_uses_2d_layout_mode_for_rack_and_area_spatial_modeling() -> None:
    assert 'if (locationEditMode)' in SOURCE
    assert 'setRackFocusId(null)' in SOURCE
    assert 'onMoveRack={moveRackDraft}' in SOURCE
    assert 'rackEditingEnabled={locationEditMode && advancedAreaMaintenanceOpen}' in SOURCE
    assert 'className="twin-rack-layout-editor"' in SOURCE
    assert "逐层设置" in SOURCE
    assert "层格数" in SOURCE
    assert "本层尚未分格" in SOURCE
    assert "level_cell_counts" in SOURCE
    assert "地图发布后同步为模具台账逐层格位" in SOURCE
    assert "添加货架" in SOURCE
    assert "删除货架" in SOURCE
    assert "区域设置" in SOURCE
    assert "区域允许存放类型" in SOURCE
    assert "成品" in SOURCE and "半成品" in SOURCE and "原材料" in SOURCE
    assert "货架＋栈板混合区" in SOURCE
    assert "/api/warehouse/twin-layout/floors/${floorCode}/racks" in SOURCE
    assert "/storage-policy" in SOURCE
    assert "员工地图、库存数量、栈板和正式库位均不改变" in SOURCE
    assert "rackEditingEnabled?: boolean" in CANVAS


def test_p1_34c_layout_edits_use_admin_draft_validation_and_explicit_publish() -> None:
    assert "/api/warehouse/twin-layout/floors/${floorCode}/draft`" in SOURCE
    assert "/draft/validate" in SOURCE
    assert "/draft/publish" in SOURCE
    assert "/draft/discard" in SOURCE
    assert "保存到布局草稿" in SOURCE
    assert "校验草稿" in SOURCE
    assert "发布布局" in SOURCE
    assert "员工仍只看到已发布地图" in SOURCE
    assert "raw.revision || raw.source_sha256" in SOURCE


def test_phase2c9_admin_operations_and_read_only_locating_share_the_measured_map() -> None:
    assert 'value.user.role === "admin"' in SOURCE
    assert 'value.permissions.includes("warehouse.stocktake.submit")' in SOURCE
    assert '"/api/warehouse/twin-operations/move-batches"' in SOURCE
    assert "移货页面草稿" in SOURCE
    assert "整栈移动" in SOURCE
    assert "canExecuteWarehouse" in SOURCE
    assert "buildMoveBatchPayload(moveBatchIdempotencyKey, moveDrafts)" in SOURCE
    assert "idempotency_key: idempotencyKey" in MOVE_DRAFT
    assert "buildStocktakeBatchPayload(stocktakeBatchIdempotencyKey, stocktakeDrafts)" in SOURCE
    assert 'inventoryType === "finished" && !["ground", "rack", "temporary_aisle"].includes(storageType)' in STOCKTAKE_DRAFT
    assert '["ground", "rack", "temporary_aisle"]' in STOCKTAKE_DRAFT
    assert "stocktakeLocationBlockReason" in STOCKTAKE_DRAFT
    assert '>前往移货</button>' in SOURCE
    assert 'setMoveAction("relocate")' in SOURCE
    assert "模具编码或名称" in SOURCE
    assert "印刷版编码、产品或位置" in SOURCE
    assert "focusedResource.prompt" in SOURCE
    assert 'const entity = { kind: "pallet" as const, id: `erp-location-${pendingLocationId}` }' in SOURCE
    assert 'setCameraFocusTarget({ entity, token: cameraFocusSequenceRef.current, source: "search" })' in SOURCE
    assert "只读定位" in SOURCE
    assert "当前是查货模式，只读真实库存和地图位置" in SOURCE
    assert "不执行入库、移货、盘点或布局写入" in SOURCE


def test_phase2c9_right_side_selection_summarizes_location_and_collapses_secondary_facts() -> None:
    assert 'const [locationDetailOpen, setLocationDetailOpen] = useState(false)' in SOURCE
    assert 'const [locationItemsExpanded, setLocationItemsExpanded] = useState(false)' in SOURCE
    assert "当前位置 · {selectedLocation.location_code}" not in SOURCE
    assert "<small>当前位置</small><b>{employeeLocationName(selectedLocation)}</b>" in SOURCE
    assert 'showInternalCodes={mapMode === "planning" && canEditLocations}' in SOURCE
    assert "showInternalCodes?: boolean" in CANVAS
    assert "货物</small><b>{selectedLocationItems.length} 条" in SOURCE
    assert "产品名称待补充" in SOURCE
    assert "客户" in SOURCE
    assert 'aria-expanded={locationDetailOpen}' in SOURCE
    assert 'locationDetailOpen ? "收起位置与栈板详情" : "位置与栈板详情"' in SOURCE
    assert 'locationDetailOpen && <div className="twin-location-secondary"' in SOURCE
    assert "<dt>位置</dt><dd>{employeeLocationName(selectedLocation)}</dd>" in SOURCE
    assert "地图状态" in SOURCE
    assert "实体栈板" in SOURCE
    assert "库存明细" in SOURCE


def test_right_side_area_summary_hides_duplicate_labels_but_keeps_full_ledger_accessible() -> None:
    assert 'const [areaInventoryDetailsOpen, setAreaInventoryDetailsOpen] = useState(false)' in SOURCE
    assert "当前区域 · {selectedAreaCode || selectedAreaFeature.feature_code}" not in SOURCE
    assert "当前规划区域 · ${selectedAreaCode || selectedAreaFeature.feature_code}" in SOURCE
    assert 'mapMode === "planning" && canEditLocations' in SOURCE
    assert "employeeAreaName(selectedAreaFeature, { floorCode })" in SOURCE
    assert "区域状态" in SOURCE
    assert "最大容量" in SOURCE
    assert "当前库存" in SOURCE
    assert "0 · 当前无货" in SOURCE
    assert "selectedAreaQuantitySummary" in SOURCE
    assert "areaInventoryDetailsOpen && <div className=\"twin-area-lot-details\"" in SOURCE
    assert "内部码 {item.location_code || \"未编\"}" not in SOURCE
    assert '{item.location_name || "位置名称待完善"}' in SOURCE
    assert "inventoryAgeLabel(item.age_days)" in SOURCE
    assert "item.location_guide?.prompt" in SOURCE
    assert "item.rack_location" not in SOURCE[SOURCE.index('className="twin-area-mold"'):SOURCE.index('className="twin-area-mold-pagination"')]


def test_p1_42b_uses_only_measured_dispatch_zones_and_keeps_transfer_targets() -> None:
    floor_one = load_warehouse_twin_floor("1F")
    assert not any(
        item["feature_code"].startswith("ZONE-1F-OUT-")
        for item in floor_one["features"]
    )
    assert {
        item["feature_code"] for item in floor_one["excluded_out_of_bounds_zones"]
    } == {
        "ZONE-1F-OUT-E-001",
        "ZONE-1F-OUT-E-002",
        "ZONE-1F-OUT-S-001",
    }
    measured_dispatch = {
        item["feature_code"] for item in floor_one["features"]
        if item.get("subtype") == "finished_wait_delivery"
    }
    assert measured_dispatch == {"ZONE-1F-FIN-001", "ZONE-1F-FIN-002", "ZONE-1F-FIN-003"}

    assert "isMeasuredDispatchFeature" in SOURCE
    assert "buildMeasuredDispatchPallets" not in SOURCE
    assert "待定位成品" in SOURCE
    inventory_source = (ROOT / "factory_twin/frontend/src/warehouseInventory.mjs").read_text(encoding="utf-8")
    assert "Borrowing a" in inventory_source
    assert "FIN polygon would falsely tell operators" in inventory_source
    assert "unlocated_inventory blocker" in inventory_source
    assert 'id: `erp-dispatch-pallet-${pallet.pallet_id}`' not in inventory_source
    assert "账上有货，但没有已发布实测格位" in SOURCE
    assert "不会借用其他区域坐标" in SOURCE
    assert "const switchWarehouseFloor" in SOURCE
    assert "来源 ${moveSource.inventory_code} 仍保留" in SOURCE
    assert 'onClick={() => switchWarehouseFloor("3F")}' in SOURCE
    assert "选择后同步切换地图" in SOURCE
    assert "继续点地图中的具体空货位" in SOURCE
    assert "在地图选择目标空货位" in SOURCE
    assert "已绑定实体栈板的货物卡拖到此处" in SOURCE
    assert "请先选来源，再按楼层、区域、具体货位" in SOURCE
    assert "选择主货位" in SOURCE
    assert "点击上方栈板或散存标签后" in SOURCE
    assert '"/api/warehouse/twin-operations/move-batches"' in SOURCE
    assert "buildMoveBatchPayload(moveBatchIdempotencyKey, moveDrafts)" in SOURCE
    assert '"/api/warehouse/pallets/merge-batches"' in SOURCE
    assert '<select value={moveTargetLocationId}' not in SOURCE
    assert 'role="radiogroup" aria-label="主货位"' in SOURCE
    assert "P1_47D_ENABLED" not in SOURCE
    assert 'canChooseProducts={canStocktake && mapMode === "move" && moveAction === "stocktake"}' in SOURCE
    assert "stocktakeLocationBlockReason(location)" in STOCKTAKE_DRAFT
    assert ".twin-map-target-thumbnail" in TWIN_CSS
    assert ".twin-dispatch-label-list" in TWIN_CSS


def test_phase2c10_keeps_location_clicks_lightweight_and_focuses_search_hits() -> None:
    assert "const handlersRef = useRef" in CANVAS
    assert "syncEntityHighlights(runtime, selected, focusTarget)" in CANVAS
    assert "animateFocus(runtime, focusTarget)" in CANVAS
    assert "0x7c3aed" in CANVAS
    assert "new THREE.BoxGeometry(Math.max(size.x, 220), 26, Math.max(size.z, 220))" in CANVAS
    assert "controls.mouseButtons.LEFT = THREE.MOUSE.PAN" in CANVAS
    assert "controls.mouseButtons.RIGHT = THREE.MOUSE.ROTATE" in CANVAS
    assert "renderer.shadowMap.enabled = viewMode === \"25d\" && !warehouseTheme" in CANVAS
    canvas_effect_dependencies = CANVAS.split("}, [layout, assets, viewMode, cameraPreset, viewResetToken, layers", 1)[1].split("]);", 1)[0]
    assert "selected" not in canvas_effect_dependencies
    assert "onSelect" not in canvas_effect_dependencies
    assert "focusTarget={cameraFocusTarget}" in SOURCE
    assert "平移：左键拖动 · 旋转：右键拖动 · 滚轮缩放" in SOURCE


def test_phase2c10_flags_column_conflicts_and_blocks_conflicting_layout_drafts() -> None:
    assert "findPalletColumnConflicts" in SOURCE
    assert 'rule_code: "LOCATION_OVERLAPS_COLUMN"' in SOURCE
    assert "货位与固定柱子重叠" in SOURCE
    assert "已阻止保存" in SOURCE
    assert "柱子冲突" in SOURCE
    assert "EMPTY_CANVAS_POINTS" in SOURCE


def test_phase2c11_adds_rack_navigation_auto_locations_and_admin_corrections() -> None:
    assert "WarehouseRackElevation" in SOURCE
    assert "rack.rack_code.match" in SOURCE
    assert "return candidate || null" in SOURCE
    assert "仓储货架正视图" in SOURCE
    assert 'area?.area_code || areaCode || "未匹配区域"' not in SOURCE
    assert "上一个同区域货架" in SOURCE
    assert "下一个同区域货架" in SOURCE
    assert "当前产品标签" in SOURCE
    assert "ERP PRODUCT LABEL" not in SOURCE
    assert "inventoryLabelQuantity(item)" in SOURCE
    assert "产品数量</dt>" in SOURCE
    assert "twin-header-area-summary" in SOURCE
    assert "selectedLocationAreaCode" in SOURCE
    assert "selectedAreaFeature?.name" in SOURCE
    assert "区域库位数量" in SOURCE
    assert "目标库位数" in SOURCE
    assert "location-count" in SOURCE
    assert "/quantity-correction" not in SOURCE
    assert '"/api/warehouse/twin-operations/stocktake-batches"' in SOURCE
    assert "buildStocktakeBatchPayload" in SOURCE
    assert 'uiMode === "large" ? "large-text" : ""' in SOURCE
    assert "location-editing" in SOURCE


def test_phase2c12_location_first_product_selection_and_collapsed_rack_details() -> None:
    assert "正式货位盘点调整" in SOURCE
    assert "为此货位选产品" in SOURCE
    assert "筛选该客户已有产品" in SOURCE
    assert "/api/warehouse/floor3/product-candidates?${params.toString()}" in SOURCE
    assert '"/api/warehouse/twin-operations/stocktake-batches"' in SOURCE
    assert "/api/warehouse/twin-operations/temporary-finished-inbound" not in SOURCE
    assert "queueStocktakeAddDraft" in SOURCE
    assert "queueStocktakeDecreaseDraft" in SOURCE
    assert "stocktakeLocationBlockReason" in STOCKTAKE_DRAFT
    assert "查看详情" in SOURCE
    assert "twin-rack-product-detail" in SOURCE


def test_p1_123_stocktake_accepts_current_map_and_guides_existing_move_before_admin_supplement() -> None:
    assert '["TWIN_V1", "CURRENT_MAP"].includes(sourceVersion)' in STOCKTAKE_DRAFT
    assert "缺少受支持的正式地图来源" not in STOCKTAKE_DRAFT
    assert "stocktakeBlockResolution" in STOCKTAKE_DRAFT
    assert "stocktakeExistingProductLocations" in STOCKTAKE_DRAFT
    assert "不可盘点调减：{stocktakeBlockReason}" in SOURCE
    assert "解决方法：{stocktakeBlockResolution(stocktakeBlockReason)}" in SOURCE
    assert "解决方法：${stocktakeBlockResolution(message)}" in SOURCE
    assert "选择本区域盘点货位" in SOURCE
    assert 'aria-label="盘点目标货位"' in SOURCE
    assert "客户全称、中文简称、缩写或客户编码" in SOURCE
    assert "存货编码、客户料号或产品名称" in SOURCE
    assert "先核对仓库现有库存" in SOURCE
    assert "先移入这里" in SOURCE
    assert "现存数量仍不足，补录缺少部分" in SOURCE
    assert "盘点补录会增加正式库存，只能由管理员确认" in SOURCE
    assert "补录会建立独立的盘点库存批次，不挂到已有订单" in SOURCE


def test_built_twin_entry_uses_versioned_assets() -> None:
    assert 'id="warehouse-twin-root"' in BUILT
    assert "/factory-twin-assets/assets/" in BUILT


def test_mold_rack_move_mode_reuses_guarded_location_move_and_exposes_publish_steps() -> None:
    assert "移动该模具" in SOURCE
    assert '"/api/warehouse/molds/location-options"' in SOURCE
    assert '"/api/warehouse/molds/location-movement/preview"' in SOURCE
    assert '"/api/warehouse/molds/location-movement/confirm"' in SOURCE
    assert "expected_version: movePreview.expected_version" in SOURCE
    assert 'idempotency_key: moveIdempotencyKey' in SOURCE
    assert "确认实物已搬动并保存" in SOURCE
    assert "当前正式层格" in SOURCE
    assert "当前草稿层格" in SOURCE
    assert "① 保存层格到草稿" in SOURCE
    assert "② 校验当前楼层草稿" in SOURCE
    assert "③ 发布当前楼层地图" in SOURCE
    assert "其他楼层草稿会独立保留" in SOURCE
    assert "只有第③步发布完成" in SOURCE
    assert "货架总层数（含设备占用层）" in SOURCE
    assert ".twin-mold-rack-planner-actions button:disabled" in TWIN_CSS
    assert ".twin-mold-move-panel" in TWIN_CSS

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx").read_text(encoding="utf-8")
CANVAS = (ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx").read_text(encoding="utf-8")
INDUSTRIAL = (ROOT / "factory_twin" / "frontend" / "src" / "industrialScene.ts").read_text(encoding="utf-8")
BUILT = (ROOT / "static" / "factory-twin-assets" / "warehouse-twin.html").read_text(encoding="utf-8")
TWIN_CSS = (ROOT / "factory_twin" / "frontend" / "src" / "warehouseTwin.css").read_text(encoding="utf-8")
ERP_INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_operational_twin_reuses_the_editor_renderer_for_2d_and_25d() -> None:
    assert 'import { EditorCanvas, type CanvasFocusTarget } from "./EditorCanvas"' in SOURCE
    assert 'visualTheme="warehouse"' in SOURCE
    assert "readOnly" in SOURCE
    assert 'setViewMode("2d")' in SOURCE
    assert 'setViewMode("25d")' in SOURCE
    assert "二维平面" in SOURCE
    assert "2.5D 等距" in SOURCE
    assert 'visualTheme?: "editor" | "warehouse"' in CANVAS


def test_operational_twin_reuses_formal_inventory_and_does_not_fake_rack_positions() -> None:
    assert "/api/warehouse/twin-dashboard/overview?days=30" in SOURCE
    assert "/api/warehouse/twin-operations/locate?${params.toString()}" in SOURCE
    assert "库存只投影到已确认区域，不虚构货架层、格或箱体坐标" not in SOURCE
    assert "暂无已建空货位" in SOURCE
    assert "/api/warehouse/pallets/${" not in SOURCE
    assert "交换平面位置不改变库存" in SOURCE or "交换二维平面位置；库存和栈板绑定未改变" in SOURCE
    assert "/api/warehouse/twin-production/layouts/" in SOURCE
    assert "只保存隔离地图库的任务ID与位置关系" in SOURCE
    assert "ERP任务、数量和状态未修改" in SOURCE


def test_operational_twin_uses_erp_session_for_real_production_and_manual_mapping() -> None:
    assert 'credentials: "same-origin"' in SOURCE
    assert "只读定位，不改数量和状态" in SOURCE
    assert "当前没有待生产任务。" in SOURCE
    assert "确认投影到地图" in SOURCE
    assert "productionProjections={productionProjection?.items || EMPTY_PRODUCTION_PROJECTIONS}" in SOURCE
    assert "raw.pallets || []" in SOURCE


def test_operational_twin_declutters_labels_and_keeps_details_in_the_inspector() -> None:
    assert "labels: false" in SOURCE
    assert 'query.get("embedded") === "1"' in SOURCE
    assert '<nav className="twin-floor-switch" aria-label="楼层切换">' in SOURCE
    assert "filterOperationalFeatures(raw.floor_code, raw.bounds_mm" in SOURCE
    assert 'onSelect={selectOperationalEntity}' in SOURCE
    assert "库存与库位" in SOURCE
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
    assert "天明智慧仓储" in SOURCE
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
    assert "区域库存筛选" in SOURCE
    assert "存货编码、产品、客户、位置" in SOURCE
    assert "地图已突出显示" in SOURCE
    assert "数据截至" in SOURCE
    assert "当前区域没有有效库存" in SOURCE
    assert "这是 ERP 当前真实空态，不生成模拟货物" in SOURCE
    assert "可用 {formatNumber(item.available_quantity ?? item.quantity)} {inventoryUnitLabel(item.unit)}" in SOURCE
    assert "已预占 {formatNumber(item.reserved_quantity)} {inventoryUnitLabel(item.unit)}" in SOURCE


def test_operational_twin_uses_cross_floor_search_highlights_and_mapped_location_pallets() -> None:
    assert "全仓查找" in SOURCE
    assert "库存编码定位" not in SOURCE
    assert "先选择查找类型" in SOURCE
    assert 'const [searchPanelOpen, setSearchPanelOpen] = useState(false)' in SOURCE
    assert "searchHighlightAreaCodes(searchHighlightItems, floorCode)" in SOURCE
    assert "highlightFeatureIds={searchHighlightFeatureIds}" in SOURCE
    assert "highlightedPalletIds={searchHighlightPalletIds}" in SOURCE
    assert "buildMappedLocationPallets(features, visualLocations, floorCode, layout?.id)" in SOURCE
    assert "pallets: [...layout.pallets, ...mappedLocationPallets]" in SOURCE
    assert '<aside className="twin-context-rail">' in SOURCE
    assert "库存与库位" in SOURCE
    assert "twin-stage-footer" not in SOURCE
    assert "twin-system-footer" not in SOURCE


def test_phase2c14_uses_typed_map_search_and_customer_first_difference_entry() -> None:
    assert 'type WarehouseSearchType = "finished" | "mold" | "printing_plate"' in SOURCE
    assert "纸箱成品" in SOURCE
    assert "模具编码或名称" in SOURCE
    assert "印刷版编码、产品或位置" in SOURCE
    assert "1　客户名称或简写" in SOURCE
    assert "2　存货编码或产品名称" in SOURCE
    assert "groupSearchProducts(searchResponse?.items || [])" in SOURCE
    assert "地图已突出显示" in SOURCE
    assert "warehouse-search-hit" in SOURCE
    assert "product-search-hit" in SOURCE
    assert "地图选点入仓 / 差异补录" in SOURCE
    assert "先在顶部选择 1F/3F" in SOURCE
    assert "筛选该客户常用箱" in SOURCE
    assert "同客户、同存货产品且类型兼容时可合并" in SOURCE
    assert "/api/warehouse/twin-operations/semi-finished-inbound" in SOURCE
    assert "selectedLocation.location_name" in SOURCE
    assert "内部库位编码只在详情中保留" in SOURCE
    assert ".twin-location-item.warehouse-search-hit" in TWIN_CSS
    assert ".twin-area-lot.product-search-hit" in TWIN_CSS


def test_phase2c8_keeps_location_layout_editing_in_2d_and_25d_read_only() -> None:
    assert "库位布局" in SOURCE
    assert "二维编辑" in SOURCE
    assert "2.5D 流畅查看 · 详情见右侧" in SOURCE
    assert "locationLayoutGeometry(" in SOURCE
    assert "/api/warehouse/floor3/layout/areas/${areaCode}" in SOURCE
    assert "/api/warehouse/floor3/layout/areas/${selectedAreaCode}/location-count" in SOURCE
    assert "系统按区域自动生成内部唯一编码" in SOURCE
    assert "/api/warehouse/floor3/layout/slots/${selectedLocation.location_id}/disable" in SOURCE
    assert "palletEditingOnly={locationEditMode}" in SOURCE
    assert "选择区域或设备" not in SOURCE


def test_phase2c13_uses_2d_layout_mode_for_rack_and_area_spatial_modeling() -> None:
    assert 'if (locationEditMode)' in SOURCE
    assert 'setRackFocusId(null)' in SOURCE
    assert 'onMoveRack={moveRackDraft}' in SOURCE
    assert 'rackEditingEnabled={locationEditMode}' in SOURCE
    assert 'className="twin-rack-layout-editor"' in SOURCE
    assert "每层货位数" in SOURCE
    assert "每层净高" in SOURCE
    assert "添加货架" in SOURCE
    assert "删除货架" in SOURCE
    assert "区域设置" in SOURCE
    assert "区域允许存放类型" in SOURCE
    assert "成品" in SOURCE and "半成品" in SOURCE and "原材料" in SOURCE
    assert "货架＋栈板混合区" in SOURCE
    assert "/api/warehouse/twin-layout/floors/${floorCode}/racks" in SOURCE
    assert "/storage-policy" in SOURCE
    assert "不会修改库存数量、栈板或正式库位身份" in SOURCE
    assert "rackEditingEnabled?: boolean" in CANVAS


def test_phase2c9_admin_operations_and_read_only_locating_share_the_measured_map() -> None:
    assert 'value.user.role === "admin"' in SOURCE
    assert "/api/warehouse/twin-operations/finished-inbound" in SOURCE
    assert "/api/warehouse/twin-operations/pallets/${selectedLocation.pallet.pallet_id}/move" in SOURCE
    assert "地图选点入仓 / 差异补录" in SOURCE
    assert "正式栈板移位" in SOURCE
    assert "仅 admin" in SOURCE
    assert "confirmed: true" in SOURCE
    assert "idempotency_key: inboundIdempotencyKey" in SOURCE
    assert "idempotency_key: moveIdempotencyKey" in SOURCE
    assert 'item.storage_type !== "rack"' in SOURCE
    assert 'selectedLocation?.storage_type !== "rack"' in SOURCE
    assert "该位置尚未启用、未完成布局、库存类型不匹配或与柱子冲突" in SOURCE
    assert "模具编码或名称" in SOURCE
    assert "印刷版编码、产品或位置" in SOURCE
    assert "focusedResource.prompt" in SOURCE
    assert 'setSelected({ kind: "pallet", id: `erp-location-${pendingLocationId}` })' in SOURCE
    assert "只读定位" in SOURCE
    assert "当前账号只可查找和定位，不可执行仓库写操作" in SOURCE


def test_phase2c9_pallet_label_prioritizes_goods_and_collapses_secondary_location_facts() -> None:
    assert 'const [locationDetailOpen, setLocationDetailOpen] = useState(false)' in SOURCE
    assert "当前栈板货物" in SOURCE
    assert "存货编码" in SOURCE
    assert "产品名称待补充" in SOURCE
    assert "产品数量" in SOURCE
    assert "客户" in SOURCE
    assert 'aria-expanded={locationDetailOpen}' in SOURCE
    assert 'locationDetailOpen ? "收起详细信息" : "详细信息"' in SOURCE
    assert 'locationDetailOpen && <div className="twin-location-secondary"' in SOURCE
    assert "区域与库位" in SOURCE
    assert "地图状态" in SOURCE
    assert "实体栈板" in SOURCE
    assert "库存明细" in SOURCE


def test_phase2c10_keeps_location_clicks_lightweight_and_focuses_search_hits() -> None:
    assert "const handlersRef = useRef" in CANVAS
    assert "syncEntityHighlights(runtime, selected, focusTarget)" in CANVAS
    assert "animateFocus(runtime, focusTarget)" in CANVAS
    assert "0xff2d8b" in CANVAS
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
    assert 'area?.area_code || areaCode || "未匹配区域"' in SOURCE
    assert "上一个同区域货架" in SOURCE
    assert "下一个同区域货架" in SOURCE
    assert "ERP PRODUCT LABEL" in SOURCE
    assert "当前产品标签" in SOURCE
    assert "inventoryLabelQuantity(item)" in SOURCE
    assert "产品数量</dt>" in SOURCE
    assert "twin-header-area-summary" in SOURCE
    assert "selectedLocationAreaCode" in SOURCE
    assert "selectedAreaFeature?.name" in SOURCE
    assert "区域库位数量" in SOURCE
    assert "目标库位数" in SOURCE
    assert "location-count" in SOURCE
    assert "/api/warehouse/twin-operations/lots/${selectedCorrectionItem.lot_id}/quantity-correction" in SOURCE
    assert "管理员二次确认" in SOURCE
    assert "移除不会物理删除批次或流水" in SOURCE
    assert 'uiMode === "large" ? "large-text" : ""' in SOURCE
    assert "location-editing" in SOURCE


def test_phase2c12_location_first_product_selection_and_collapsed_rack_details() -> None:
    assert "地图选点入仓 / 差异补录" in SOURCE
    assert "为此货位选产品" in SOURCE
    assert "已完工未送" in SOURCE
    assert "/api/warehouse/twin-operations/location-product-candidates" in SOURCE
    assert "/api/warehouse/twin-operations/staging-lots/${selectedStagingProduct.lot_id}/place" in SOURCE
    assert "/api/warehouse/twin-operations/temporary-finished-inbound" in SOURCE
    assert "临时新产品" in SOURCE
    assert "临时建档原因（必填）" in SOURCE
    assert "本次只移动原库存位置，不增加库存总数" in SOURCE
    assert "查看详情" in SOURCE
    assert "twin-rack-product-detail" in SOURCE


def test_built_twin_entry_uses_versioned_assets() -> None:
    assert 'id="warehouse-twin-root"' in BUILT
    assert "/factory-twin-assets/assets/" in BUILT

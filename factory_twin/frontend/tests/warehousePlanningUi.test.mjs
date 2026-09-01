import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/WarehouseTwinApp.tsx", import.meta.url), "utf8");
const editorSource = readFileSync(new URL("../src/EditorCanvas.tsx", import.meta.url), "utf8");
const sceneSource = readFileSync(new URL("../src/industrialScene.ts", import.meta.url), "utf8");
const inventorySource = readFileSync(new URL("../src/warehouseInventory.mjs", import.meta.url), "utf8");
const cssSource = readFileSync(new URL("../src/warehouseTwin.css", import.meta.url), "utf8");

test("ordinary area planning exposes a current-area-only point editing workflow", () => {
  assert.doesNotMatch(source, />拖动并保存现场货位</);
  assert.match(source, />保存并固定/);
  assert.match(source, />取消点位调整/);
  assert.match(source, /只有点击区域空白处才选择区域/);
  assert.match(source, /location\?\.area_code !== locationPointEditAreaCode/);
  assert.match(source, /draggablePalletIds=\{warehouseMoveModeActive \? movablePalletIds : layoutMapToolsOpen \? \[\] : locationPointEditPalletIds\}/);
  assert.match(source, /mergePublishedFeatureGeometry/);
});

test("point save is the single explicit action and keeps inventory outside the write scope", () => {
  assert.match(source, /有货货位请先按现场实际核对/);
  assert.match(source, /保存会同步权威排位，但不改库存、栈板绑定或数量/);
  assert.doesNotMatch(source, /确认保存并固定/);
  assert.doesNotMatch(source, /occupiedDraftCount/);
});

test("area planning gives a clicked location priority over its enclosing area", () => {
  assert.match(editorSource, /const preferredPlanningPallet = palletEditingOnly/);
  assert.match(editorSource, /candidate\.userData\.entityKind === "pallet" && candidate\.userData\.draggable/);
  assert.match(editorSource, /preferredPlanningPallet \|\| preferredPlanningFeature \|\| roots\[0\]/);
  assert.match(source, /if \(!locationEditMode \|\| layoutMapToolsOpen\) return/);
  assert.match(source, /setLocationPointEditAreaCode\(location\.area_code\)/);
  assert.match(source, /palletEditingOnly=\{locationEditMode \|\| warehouseMoveModeActive\}/);
});

test("area planning shows every ground location with a full colored slot footprint", () => {
  assert.match(source, /layout\?\.id,\s*locationEditMode/);
  assert.match(source, /区域规划会按已发布容量显示全部正式货位/);
  assert.match(source, /绿色为空货位，蓝色为有货货位/);
  assert.match(inventorySource, /renderEmptyPlanningSlots && isGroundLocation/);
  assert.match(inventorySource, /planning_slot_width_mm: isPlanningLocationSlot \? standard\.width_mm/);
  assert.match(inventorySource, /planning_slot_depth_mm: isPlanningLocationSlot \? standard\.depth_mm/);
  assert.match(sceneSource, /pallet\.is_planning_location_slot/);
  assert.match(sceneSource, /new THREE\.EdgesGeometry\(geometry\)/);
});

test("published ground positions use the dedicated atomic save contract", () => {
  assert.match(source, /available_actions\.includes\("published_layout"\)/);
  assert.match(source, /ground-layout\/floors\/\$\{encodeURIComponent\(floorCode\)\}\/areas\/\$\{encodeURIComponent\(areaCode\)\}\/published-positions/);
  assert.match(source, /expected_plan_version: management\.ground_plan_version/);
  assert.match(source, /idempotency_key: locationLayoutIdempotencyKey/);
  assert.match(source, /查货、移货、盘点和空货位显示将统一使用这组位置/);
});

test("map-first toolbar hides empty delayed dispatch and consolidates selective merge", () => {
  assert.match(source, /const \[delayedDispatchOpen, setDelayedDispatchOpen\] = useState\(false\)/);
  assert.match(source, /!!dashboard\?\.delayed_dispatch_relocation\?\.candidate_count/);
  assert.match(source, /延期待送 \{dashboard\.delayed_dispatch_relocation\.candidate_count\}/);
  assert.match(source, /delayedDispatchOpen && dashboard\?\.delayed_dispatch_relocation/);
  assert.doesNotMatch(source, />自动合并</);
  assert.match(source, /onClick=\{openAutomaticMerge\}>合并栈板</);
  assert.match(source, />可合并货位</);
  assert.doesNotMatch(source, /只勾选现场要合并的栈板/);
  assert.match(source, /type="checkbox" checked=\{selected\}/);
});

test("warehouse header exposes the formal location label printing entry", () => {
  assert.match(source, /href="\/warehouse-ledger\.html\?tab=locations&amp;location_view=ledger&amp;label_print=1"/);
  assert.match(source, />打印货位编号<\/a>/);
});

test("warehouse header keeps both label and ledger links on the command row", () => {
  assert.match(cssSource, /grid-template-columns:\s*minmax\(680px, 1fr\) auto auto auto/);
  assert.match(cssSource, /@media \(max-width: 1180px\)[\s\S]*grid-template-columns:\s*minmax\(470px, 1fr\) auto auto/);
});

test("lookup has one entry and area planning uses short adaptive actions", () => {
  const toolbar = source.slice(source.indexOf('<section className="twin-toolbar">'), source.indexOf('<section className={`twin-workspace'));
  assert.doesNotMatch(toolbar, /twin-warehouse-search-toggle/);
  assert.match(source, /<header><h2>查货<\/h2><\/header>/);
  assert.match(source, /<b>用途与容量<\/b>/);
  assert.match(source, /: "确认"\}<\/button>/);
  assert.match(source, />编辑<\/button>/);
  assert.match(source, />货位\/货架<\/button>/);
  assert.match(source, />发布<\/button>/);
  assert.doesNotMatch(source, />确认并启用此区域<\/button>/);
});

test("area planning keeps short inputs in compact rows", () => {
  assert.match(source, /className="twin-zone-primary-fields"/);
  assert.match(source, /className="twin-zone-confirm-row"/);
  assert.match(source, /className="twin-zone-policy-fields"/);
  assert.match(source, /className="twin-rack-primary-fields"/);
  assert.match(source, /className="twin-mold-rack-fields"/);
  assert.match(cssSource, /\.twin-zone-primary-fields,[\s\S]*\.twin-mold-rack-fields\s*\{[\s\S]*repeat\(auto-fit, minmax\(100px, 1fr\)\)/);
  assert.match(cssSource, /\.twin-zone-geometry-grid\s*\{[\s\S]*repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(cssSource, /\.twin-ground-layout-grid\s*\{[\s\S]*repeat\(3, minmax\(0, 1fr\)\)/);
});

test("planning dimensions save the latest input and adjustment locks map panning", () => {
  assert.match(source, /const \[layoutMapToolsOpen, setLayoutMapToolsOpen\] = useState\(false\)/);
  assert.match(source, /const zoneGeometryDraftsRef = useRef<Record<string, number\[\]\[\]>>\(\{\}\)/);
  assert.match(source, /zoneGeometryDraftsRef\.current\[selectedAreaFeature\.id\]/);
  assert.match(source, /mapPanLocked=\{locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust"\}/);
  assert.match(editorSource, /mapPanLocked\?: boolean/);
  assert.match(editorSource, /controls\.enablePan = !mapPanLocked/);
});

test("planning exits to lookup and map geometry tools open only on demand", () => {
  assert.match(source, /setMapMode\("lookup"\);[\s\S]*setSearchPanelOpen\(true\)/);
  assert.match(source, /\{layoutMapToolsOpen \? "完成地图调整" : "调整地图"\}<\/button>/);
  assert.match(source, /setLayoutMapToolsOpen\(\(current\) => !current\)/);
  assert.match(source, /mapMode === "planning" && locationEditMode && canEditLocations && layoutMapToolsOpen && <section className="twin-layout-map-tools">/);
  assert.match(source, /featureEditingEnabled=\{locationEditMode && layoutMapToolsOpen && !locationPointEditAreaCode && layoutMapTool === "adjust"\}/);
  assert.match(source, /layoutDrawKind = locationEditMode && layoutMapToolsOpen && layoutMapTool !== "adjust"/);
});

test("planning moves empty zones but locks boundaries that own formal locations", () => {
  assert.match(editorSource, /planningFeatureEditable[\s\S]*\["zone", "aisle"\]\.includes\(feature\.feature_kind\)/);
  assert.doesNotMatch(
    editorSource.slice(editorSource.indexOf("const planningFeatureEditable"), editorSource.indexOf("group.userData =", editorSource.indexOf("const planningFeatureEditable"))),
    /feature\.is_locked/
  );
  assert.match(source, /mapPanLocked=\{locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust"\}/);
  assert.match(source, /rackEditingEnabled=\{locationEditMode && layoutMapToolsOpen && advancedAreaMaintenanceOpen\}/);
  assert.match(editorSource, /preferredPlanningFeature[\s\S]*candidate\.userData\.entityKind === "feature" && candidate\.userData\.draggable/);
  assert.match(source, /featureHasMappedGroundLocations/);
  assert.match(source, /区域边界已锁定，不能带着货位一起移动或缩放/);
  assert.match(source, /disabled=\{spatialEditBusy \|\| selectedAreaBoundaryLocked\}/);
});

test("map adjustment and location placement are mutually exclusive", () => {
  assert.match(source, /const locationProjectionFeatures = useMemo/);
  assert.match(source, /\(\) => mergePublishedFeatureGeometry\(features, planningPublishedFeatures\)/);
  assert.match(source, /setLayoutMapToolsOpen\(false\)/);
  assert.match(source, /layoutMapToolsOpen \? \[\] : locationPointEditPalletIds/);
  assert.match(source, /!locationPointEditAreaCode && layoutMapTool === "adjust"/);
  assert.match(source, /activeLocationDraftCount > 0/);
  assert.match(source, /if \(layoutMapToolsOpen\)/);
});

test("warehouse racks use exact footprint picking and only horizontal or vertical direction", () => {
  assert.match(editorSource, /function warehouseRackPickProxy\(rack: Rack\)/);
  assert.match(editorSource, /new THREE\.BoxGeometry\(width, pickHeight, depth\)/);
  assert.match(editorSource, /warehouseRackPickProxy\(rack\)/);
  assert.match(sceneSource, /if \(!warehouseTheme\) \{[\s\S]*accessDirectionVectors\(rack\.access_side\)/);
  const direction = source.slice(source.indexOf('aria-label="货架方向"'), source.indexOf('</select>', source.indexOf('aria-label="货架方向"')));
  assert.match(direction, /横向 0°/);
  assert.match(direction, /竖向 90°/);
  assert.doesNotMatch(direction, /180°|270°/);
  assert.doesNotMatch(source, /<span>正面方向<\/span>/);
});

test("rack focus keeps the map visible beside an ERP styled elevation", () => {
  assert.match(source, /focusedRack \? "rack-focused" : ""/);
  assert.match(source, /className="twin-map-pane"/);
  assert.match(source, /className="twin-rack-map-callout"/);
  assert.match(source, /className="twin-rack-focus-panel twin-rack-stage/);
  assert.doesNotMatch(source, /className="twin-rack-modal"/);
  assert.match(cssSource, /\.twin-stage\.rack-focused\s*\{[\s\S]*grid-template-columns:\s*minmax\(260px, 1fr\) minmax\(0, 2fr\)/);
  assert.match(cssSource, /@media \(max-width: 880px\)[\s\S]*\.twin-stage\.rack-focused\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
});

test("planning uses one contextual delete action for selected zones or aisles", () => {
  assert.match(source, /deleteSelectedLayoutFeature/);
  assert.match(source, /\/features\/\$\{selectedLayoutFeature\.id\}\?expected_revision=/);
  assert.match(source, /selectedLayoutFeature\.feature_kind === "aisle" \? "删除通道" : "删除区域"/);
  assert.doesNotMatch(source, />新增区域<\/button>/);
  assert.doesNotMatch(source, />新增通道<\/button>/);
});

test("ordinary planning no longer offers tight automatic pallet packing", () => {
  assert.doesNotMatch(source, />自动均匀排布空闲系统货位</);
  assert.match(source, /系统不再强制把栈板紧贴均匀排布/);
});

test("count changes send the complete location snapshot even when it is empty", () => {
  assert.match(source, /target_count: targetCount,[\s\S]*expected_layout_versions: selectedAreaLayoutVersions/);
  assert.doesNotMatch(source, /expected_layout_versions: selectedAreaLocationCount \?/);
});

test("logical map positions keep a small renderer anchor", () => {
  assert.match(sceneSource, /pallet\.is_logical_anchor \? 180 : 400/);
});

test("unlocated finished blocker renders the complete backend list with physical quantities", () => {
  assert.match(source, /unlocatedFinishedItems\.map\(\(item\)/);
  assert.doesNotMatch(source, /unlocatedFinishedItems\.slice\(/);
  assert.match(source, /unlocated_inventory \|\| \[\]\)\.filter\(\(item\) => inventoryHasPhysicalQuantity\(item\)\)/);
  assert.match(source, /inventoryPhysicalQuantity\(item\)/);
});

test("dashboard locations normalize stale empty pallet wrappers before rendering and moving", () => {
  assert.match(source, /normalizeInventoryLocationProjection\(location\)/);
  assert.match(source, /currentFloorOccupiedLocations/);
  assert.doesNotMatch(source, /currentFloor\?\.occupied_locations \|\| 0/);
});

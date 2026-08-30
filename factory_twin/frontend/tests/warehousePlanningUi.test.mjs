import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/WarehouseTwinApp.tsx", import.meta.url), "utf8");
const sceneSource = readFileSync(new URL("../src/industrialScene.ts", import.meta.url), "utf8");

test("ordinary area planning exposes a current-area-only point editing workflow", () => {
  assert.match(source, />拖动并保存现场货位</);
  assert.match(source, />保存并固定/);
  assert.match(source, />取消点位调整/);
  assert.match(source, /location\?\.area_code !== locationPointEditAreaCode/);
  assert.match(source, /draggablePalletIds=\{warehouseMoveModeActive \? movablePalletIds : locationPointEditPalletIds\}/);
});

test("point save warns about occupied locations and keeps inventory outside the write scope", () => {
  assert.match(source, /占用货位请先按现场实际/);
  assert.match(source, /保存会同步权威排位，但不改库存、栈板绑定或数量/);
  assert.match(source, /确认保存并固定/);
  assert.match(source, /其中 \$\{occupiedDraftCount\} 个为占用货位/);
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

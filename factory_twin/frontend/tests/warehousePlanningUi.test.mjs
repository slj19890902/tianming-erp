import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/WarehouseTwinApp.tsx", import.meta.url), "utf8");
const sceneSource = readFileSync(new URL("../src/industrialScene.ts", import.meta.url), "utf8");

test("ordinary area planning exposes a current-area-only point editing workflow", () => {
  assert.match(source, />调整货位点位</);
  assert.match(source, />保存并固定/);
  assert.match(source, />取消点位调整/);
  assert.match(source, /location\?\.area_code !== locationPointEditAreaCode/);
  assert.match(source, /draggablePalletIds=\{warehouseMoveModeActive \? movablePalletIds : locationPointEditPalletIds\}/);
});

test("point save warns about occupied locations and keeps inventory outside the write scope", () => {
  assert.match(source, /占用货位请先按现场实际/);
  assert.match(source, /保存只更新地图点位，不移动库存、栈板或货物/);
  assert.match(source, /确认保存并固定/);
  assert.match(source, /其中 \$\{occupiedDraftCount\} 个为占用货位/);
});

test("auto arrange requires explicit confirmation and posts the confirmed area request", () => {
  assert.match(source, /自动均匀排布空闲系统货位/);
  assert.match(source, /window\.confirm\(`确认自动均匀排布/);
  assert.match(source, /areas\/\$\{encodeURIComponent\(selectedAreaCode\)\}\/auto-arrange/);
  assert.match(source, /mutateJson<\{ message\?: string; auto_arranged_count\?: number \}>\(endpoint, "POST", \{/);
  assert.match(source, /confirmed: true,[\s\S]*expected_map_revision:[\s\S]*expected_policy_version: areaLocationManagement\?\.policy_version \|\| undefined,[\s\S]*expected_layout_versions: selectedAreaLayoutVersions/);
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
  assert.match(source, /inventoryPhysicalQuantity\(item\)/);
});

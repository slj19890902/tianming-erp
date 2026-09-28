import test from "node:test";
import assert from "node:assert/strict";
import {
  normalizeWarehouseWorkspaceUrl,
  warehouseWorkspaceActivation,
  warehouseWorkspaceBlockMessage,
  warehouseWorkspaceNavigateMessage
} from "../src/warehouseWorkspaceBridge.mjs";

const ORIGIN = "https://erp.example.test";

test("workspace navigation only accepts the two same-origin warehouse pages", () => {
  assert.equal(normalizeWarehouseWorkspaceUrl("/warehouse-ledger.html?tab=finished&q=R026", ORIGIN), "/warehouse-ledger.html?tab=finished&q=R026");
  assert.equal(normalizeWarehouseWorkspaceUrl(`${ORIGIN}/warehouse.html?floor=3F`, ORIGIN), "/warehouse.html?floor=3F");
  assert.equal(normalizeWarehouseWorkspaceUrl("https://other.test/warehouse.html", ORIGIN), null);
  assert.equal(normalizeWarehouseWorkspaceUrl("/orders.html", ORIGIN), null);
});

test("activation distinguishes absent parameters from explicit values", () => {
  assert.deepEqual(warehouseWorkspaceActivation("/warehouse.html", ORIGIN), {
    url: "/warehouse.html",
    pathname: "/warehouse.html",
    q: undefined,
    floor: undefined,
    locationId: undefined,
    lotId: undefined,
    tab: undefined,
    action: undefined
  });
  assert.deepEqual(warehouseWorkspaceActivation("/warehouse.html?q=%E5%A4%A9%E6%98%8E&floor=3F&location_id=12&lot_id=30&action=stocktake", ORIGIN), {
    url: "/warehouse.html?q=%E5%A4%A9%E6%98%8E&floor=3F&location_id=12&lot_id=30&action=stocktake",
    pathname: "/warehouse.html",
    q: "天明",
    floor: "3F",
    locationId: 12,
    lotId: 30,
    tab: undefined,
    action: "stocktake"
  });
  assert.equal(warehouseWorkspaceActivation("/warehouse.html?q=", ORIGIN).q, "", "an explicit empty query must clear the committed map search");
});

test("guard blocks active or uncertain writes but not preserved drafts", () => {
  const idle = { moveSubmitting: false, stocktakeSubmitting: false, mergeSubmitting: false, otherSubmitting: false,
    moveUncertain: false, mergeUncertain: false, pendingUncertain: false, stocktakeUncertain: false,
    stocktakeRefreshRequired: false, pendingRefreshRequired: false };
  assert.equal(warehouseWorkspaceBlockMessage(idle), "");
  assert.match(warehouseWorkspaceBlockMessage({ ...idle, moveSubmitting: true }), /正在提交/);
  assert.match(warehouseWorkspaceBlockMessage({ ...idle, moveUncertain: true }), /尚未确认/);
  assert.match(warehouseWorkspaceBlockMessage({ ...idle, mergeUncertain: true }), /尚未确认/);
  assert.match(warehouseWorkspaceBlockMessage({ ...idle, pendingUncertain: true }), /尚未确认/);
  assert.match(warehouseWorkspaceBlockMessage({ ...idle, stocktakeUncertain: true }), /尚未确认/);
  assert.equal(warehouseWorkspaceBlockMessage({ ...idle, rackOperationBlocked: "模具移动结果待核对" }), "模具移动结果待核对");
});

test("child navigation omits request id while host request replies preserve it", () => {
  const childClick = warehouseWorkspaceNavigateMessage("/warehouse-ledger.html?tab=finished");
  assert.deepEqual(childClick, {
    source: "tianming-warehouse",
    type: "warehouse-workspace-navigate",
    url: "/warehouse-ledger.html?tab=finished"
  });
  assert.equal(Object.hasOwn(childClick, "request_id"), false);
  assert.deepEqual(warehouseWorkspaceNavigateMessage("/warehouse.html?q=R026", "shell-17"), {
    source: "tianming-warehouse",
    type: "warehouse-workspace-navigate",
    url: "/warehouse.html?q=R026",
    request_id: "shell-17"
  });
});

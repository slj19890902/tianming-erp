import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(new URL("../src/WarehouseTwinApp.tsx", import.meta.url), "utf8");
const component = source.slice(source.indexOf("function WarehouseRackElevation("), source.indexOf("export function WarehouseTwinApp()"));
const js = ts.transpileModule(component, {compilerOptions: {jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022}}).outputText;
const rack = {id: "rack-f9", rack_code: "F9", levels: 3, level_cell_counts: [3, 3, 3]};
const locations = Array.from({length: 9}, (_, index) => ({
  location_id: 101 + index, map_rack_id: rack.id, level_no: Math.floor(index / 3) + 1,
  slot_no: index % 3 + 1, location_name: `F9第${Math.floor(index / 3) + 1}层第${index % 3 + 1}格`, items: [],
}));
const sandbox = {
  React: {createElement: (type, props, ...children) => ({type, props: props || {}, children: children.flat(Infinity)})},
  useState: value => [value, () => {}], useMemo: fn => fn(), useEffect() {},
  rackLevelCellCounts: value => value.level_cell_counts,
  rackCellIdentityKey: (id, level, slot) => `${id}/${level}/${slot}`,
  rackLocationInventoryItems: location => location.items,
  stocktakeAddBlockReason: location => location.blockReason || null,
  moldRackEmployeeName: value => value.rack_code, formatNumber: String,
  employeeCustomerName: () => "测试客户", inventoryLabelQuantity: () => 1, inventoryUnitLabel: String,
};
vm.createContext(sandbox);
vm.runInContext(js, sandbox);
function render(overrides = {}) {
  const selected = [];
  const tree = sandbox.WarehouseRackElevation({rack, locations, canChooseProducts: true,
    rackIndex: 0, rackCount: 1, unboundLocationCount: 0,
    onPrevious() {}, onNext() {}, onClose() {}, onChooseEmptyLocation: id => selected.push(id), ...overrides});
  function nodes(node) { return node && typeof node === "object" ? [node, ...node.children.flatMap(nodes)] : []; }
  const emptyControls = nodes(tree).filter(node => node.props.className?.includes("mold-rack-empty-spine"));
  return {selected, emptyControls};
}

test("clicking the visible plus and empty body selects each exact rack cell once", () => {
  const {selected, emptyControls} = render();
  assert.equal(emptyControls.length, 9);
  for (const button of emptyControls) {
    assert.equal(button.type, "button", "the visible plus must be an actual interactive control");
    assert.equal(button.props.type, "button");
    assert.equal(Boolean(button.props.disabled), false);
    assert.ok(button.props["aria-label"]?.includes("选产品"));
    button.props.onClick();
  }
  assert.deepEqual(selected, [107, 108, 109, 104, 105, 106, 101, 102, 103]);
});

test("missing permission, missing formal identity, duplicate identity and blocked location cannot select", () => {
  for (const overrides of [
    {canChooseProducts: false}, {locations: []},
    {locations: locations.flatMap(row => [row, {...row, location_id: row.location_id + 1000}])},
    {locations: locations.map(row => ({...row, blockReason: "规划尚未发布"}))},
  ]) {
    const {selected, emptyControls} = render(overrides);
    assert.equal(emptyControls.length, 9);
    for (const button of emptyControls) {
      assert.equal(button.type, "button");
      assert.equal(button.props.disabled, true);
      button.props.onClick(); // The callback itself also fails closed.
    }
    assert.deepEqual(selected, []);
  }
});

test("an occupied cell retains its inventory and does not render a misleading add control", () => {
  const {emptyControls} = render({locations: locations.map(row => ({...row, items: [{lot_id: row.location_id}]}))});
  assert.equal(emptyControls.length, 0);
});

test("the selected empty cell opens its stocktake inspector without writing inventory", () => {
  const callback = source.slice(source.indexOf("  const chooseRackEmptyLocation ="), source.indexOf("  const switchWarehouseFloor ="));
  const code = ts.transpileModule(callback, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const allowed of [true, false]) {
    const actions = [];
    const context = {canStocktake: allowed, mapMode: "move", moveSource: null, spatialEditBusy: false,
      cameraFocusSequenceRef: {current: 0}, inspectorRef: {current: {scrollIntoView() {}}},
      requestAnimationFrame: fn => fn(),
      ...Object.fromEntries(["setRackFocusId", "setViewMode", "setMoveAction", "setSelected", "setLocationDetailOpen", "setCameraFocusTarget"].map(name => [name, value => actions.push([name, value])])),
    };
    vm.runInNewContext(code + "\nchooseRackEmptyLocation(107);", context);
    if (allowed) {
      assert.ok(actions.some(([name, value]) => name === "setMoveAction" && value === "stocktake"));
      assert.ok(actions.some(([name, value]) => name === "setSelected" && value.id === "erp-location-107"));
      assert.ok(actions.some(([name, value]) => name === "setLocationDetailOpen" && value === true));
    } else assert.deepEqual(actions, []);
  }
});

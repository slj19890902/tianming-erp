import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { groupShelfProducts, shelfStockDates } from "../src/shelfDisplay.mjs";

const source = fs.readFileSync(new URL("../src/WarehouseTwinApp.tsx", import.meta.url), "utf8");
const component = source.slice(source.indexOf("function WarehouseRackElevation("), source.indexOf("export function WarehouseTwinApp()"));
const js = ts.transpileModule(component, {compilerOptions: {jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022}}).outputText;
const rack = {id: "rack-f9", rack_code: "F9", levels: 3, level_cell_counts: [3, 3, 3]};
const locations = Array.from({length: 9}, (_, index) => ({
  location_id: 101 + index, map_rack_id: rack.id, level_no: Math.floor(index / 3) + 1,
  slot_no: index % 3 + 1, location_name: `F9第${Math.floor(index / 3) + 1}层第${index % 3 + 1}格`, items: [],
}));
const sandbox = {
  window: {addEventListener() {}, removeEventListener() {}},
  ShelfLotHistory: () => null,
  requestJson: () => { throw new Error('interaction fixture must not request business data'); },
  groupShelfProducts,
  shelfStockDates,
  React: {createElement: (type, props, ...children) => ({type, props: props || {}, children: children.flat(Infinity)})},
  useState: value => [value, () => {}], useMemo: fn => fn(), useEffect() {}, useRef: () => ({current:null}),
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
  const inspected = [];
  const tree = sandbox.WarehouseRackElevation({rack, locations, canChooseProducts: true,
    rackIndex: 0, rackCount: 1, unboundLocationCount: 0,
    onPrevious() {}, onNext() {}, onClose() {}, onSelectLocation: id => inspected.push(id), onChooseEmptyLocation: id => selected.push(id), ...overrides});
  function nodes(node) { return node && typeof node === "object" ? [node, ...node.children.flatMap(nodes)] : []; }
  const emptyControls = nodes(tree).filter(node => node.props.className?.includes("mold-rack-empty-spine"));
  return {selected, inspected, emptyControls, nodes: nodes(tree)};
}

test('search highlights only matching products and exact cells, and replaces old highlights for another product', () => {
  const stocked = locations.map((row,index) => ({...row, items:[{lot_id:index+1,product_id:index+1,inventory_code:`CODE-${index+1}`,unit:'pcs'}]}));
  stocked[0].items.push({lot_id:50,product_id:50,inventory_code:'OTHER',unit:'pcs'});
  for (const [ids, targetLocation] of [[[1,4],101], [[9],109], [[],null]]) {
    const {nodes} = render({locations:stocked,highlightedLotIds:ids,searchLocationId:targetLocation,searchLotId:ids[0]});
    const hits = nodes.filter(n => n.type==='section' && n.props.className?.includes('rack-search-hit'));
    assert.equal(hits.length, ids.length);
    assert.equal(nodes.filter(n => n.props['data-search-current']===true).length, ids.length ? 1 : 0);
    assert.equal(nodes.filter(n => n.props.className?.includes('search-product-hit')).length,ids.length);
    if (ids.length) {
      const current = hits.find(n=>n.props['data-search-current']);
      assert.ok(current.props.title.includes(targetLocation===101?'第1层第1格':'第3层第3格'));
    }
  }
  const {nodes} = render({locations:stocked.flatMap(row=>[row,{...row,location_id:row.location_id+1000}]),highlightedLotIds:[1]});
  assert.equal(nodes.filter(n=>n.props.className?.includes('rack-search-hit')).length,0,'ambiguous formal cells must never be marked as an exact location');
});

test('switching a search hit on the same rack updates the product label and clears previous batch expansion', () => {
  const originalState = sandbox.useState, originalEffect = sandbox.useEffect;
  const states = []; let index = 0;
  sandbox.useState = initial => {
    const slot=index++;
    if (!(slot in states)) states[slot]=initial;
    return [states[slot],value=>{states[slot]=typeof value==='function'?value(states[slot]):value;}];
  };
  sandbox.useEffect = (fn,deps) => { if (deps.length===3) fn(); };
  const items=[{lot_id:71,product_id:7},{lot_id:72,product_id:8}];
  try {
    for (const item of items) {
      index=0; states[1]=true; states[2]={oldProduct:true};
      render({locations:[{...locations[0],items}],searchLotId:item.lot_id,searchLocationId:101,highlightedLotIds:[item.lot_id]});
      assert.equal(states[0],item);
      assert.equal(states[1],false);
      assert.equal(Object.keys(states[2]).length,0);
    }
  } finally { sandbox.useState=originalState; sandbox.useEffect=originalEffect; }
});

test("cell headings and non-action content select the formal location even for read-only users", () => {
  for (const occupied of [false, true]) {
    const {nodes, inspected, selected} = render({canChooseProducts: false,
      locations: locations.map(row => ({...row, items: occupied ? [{lot_id: row.location_id}] : []}))});
    for (const button of nodes.filter(n => n.props.className === 'mold-rack-cell-summary')) {
      assert.equal(Boolean(button.props.disabled), false);
      button.props.onClick();
    }
    assert.deepEqual(inspected, [107, 108, 109, 104, 105, 106, 101, 102, 103]);
    const cell = nodes.find(n => n.type === 'section' && n.props.className?.startsWith('mold-rack-cell '));
    cell.props.onClick({target: {closest: () => null}});
    assert.equal(inspected.at(-1), 107);
    assert.equal(inspected.length, 10);
    // Bubbling from label/details/print/add controls must not select or close the cell again.
    for (const tag of ['button', 'a', 'input', 'select', 'textarea', 'summary', 'details']) {
      cell.props.onClick({target: {closest: selector => selector.split(',').map(s => s.trim()).includes(tag) ? {} : null}});
    }
    assert.equal(inspected.length, 10);
    assert.deepEqual(selected, [], 'read-only navigation must not open the add-stock action');
  }
});

test("cell navigation rejects missing or conflicting identities even when occupied", () => {
  for (const occupied of [false, true]) {
    const rows = locations.map(row => ({...row, items: occupied ? [{lot_id: row.location_id}] : []}));
    for (const broken of [[], rows.flatMap(row => [row, {...row, location_id: row.location_id + 1000}])]) {
      const {nodes, inspected} = render({locations: broken});
      for (const button of nodes.filter(n => n.props.className === 'mold-rack-cell-summary')) {
        assert.equal(button.props.disabled, true);
        button.props.onClick();
      }
      for (const cell of nodes.filter(n => n.type === 'section' && n.props.className?.startsWith('mold-rack-cell '))) {
        cell.props.onClick({target: {closest: () => null}});
      }
      assert.deepEqual(inspected, []);
    }
  }
});

test("cell selection collapses the elevation and focuses the existing inspector without changing mode or drafts", () => {
  assert.match(source, /onSelectLocation=\{selectRackLocation\}/);
  const callback = source.slice(source.indexOf('  const selectRackLocation ='), source.indexOf('  const chooseRackEmptyLocation ='));
  const code = ts.transpileModule(callback, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const mapMode of ['browse', 'move']) for (const moveAction of ['relocate', 'stocktake', 'merge']) {
    for (const blocked of ['none', 'busy', 'missing']) {
      const actions = [];
      const context = {mapMode, moveAction, spatialEditBusy: blocked === 'busy', focusedRackLocations: locations,
        selectOperationalEntity: value => actions.push(['select', value.id]),
        inspectorRef: {current: {focus: () => actions.push(['focus']), scrollIntoView: () => actions.push(['scroll'])}},
        requestAnimationFrame: fn => fn(),
        setRackFocusId: value => actions.push(['rack', value]),
        setLocationDetailOpen: value => actions.push(['detail', value]),
      };
      vm.runInNewContext(code + `\nselectRackLocation(${blocked === 'missing' ? 999 : 107});`, context);
      assert.deepEqual(actions, blocked === 'none'
        ? [['select', 'erp-location-107'], ['rack', null], ['detail', false], ['focus'], ['scroll']]
        : []);
    }
  }
});

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

test("missing permission or blocked policy permits read-only inspection but never adds; invalid identities cannot select", () => {
  for (const overrides of [
    {canChooseProducts: false}, {locations: []},
    {locations: locations.flatMap(row => [row, {...row, location_id: row.location_id + 1000}])},
    {locations: locations.map(row => ({...row, blockReason: "规划尚未发布"}))},
  ]) {
    const {selected, inspected, emptyControls} = render(overrides);
    const canInspect = overrides.canChooseProducts === false || overrides.locations?.[0]?.blockReason;
    assert.equal(emptyControls.length, 9);
    for (const button of emptyControls) {
      assert.equal(button.type, "button");
      assert.equal(button.props.disabled, !canInspect);
      button.props.onClick(); // The callback itself also fails closed.
    }
    assert.deepEqual(selected, []);
    assert.deepEqual(inspected, canInspect ? [107, 108, 109, 104, 105, 106, 101, 102, 103] : []);
  }
});

test("occupied cells keep their products and can add another product to the exact formal location", () => {
  const {emptyControls, nodes, selected} = render({locations: locations.map(row => ({...row, items: [{lot_id: row.location_id}]}))});
  assert.equal(emptyControls.length, 0);
  const controls = nodes.filter(node => node.props.className === "shelf-cell-add-product");
  assert.equal(controls.length, 9);
  const headings = nodes.filter(node => node.props.className === "shelf-cell-heading");
  assert.equal(headings.filter(node => node.children.some(child => child?.props?.className === "shelf-cell-add-product")).length, 9);
  for (const button of controls) {
    assert.equal(Boolean(button.props.disabled), false);
    button.props.onClick();
  }
  assert.deepEqual(selected, [107, 108, 109, 104, 105, 106, 101, 102, 103]);
  assert.equal(nodes.filter(node => node.props.className === "shelf-product-card").length, 9);
});

test("occupied cells cannot bypass permissions, blocked policy or duplicate location identity", () => {
  const occupied = locations.map(row => ({...row, items: [{lot_id: row.location_id}]}));
  for (const overrides of [
    {locations: occupied, canChooseProducts: false},
    {locations: occupied.map(row => ({...row, blockReason: "规划尚未发布"}))},
    {locations: occupied.flatMap(row => [row, {...row, location_id: row.location_id + 1000}])},
  ]) {
    const {nodes, selected} = render(overrides);
    for (const button of nodes.filter(node => node.props.className === "shelf-cell-add-product")) {
      assert.equal(button.props.disabled, true);
      button.props.onClick();
    }
    assert.deepEqual(selected, []);
  }
});

test("carton cells prioritize code before product details and retain every batch", () => {
  const items = [1, 2].map(lot_id => ({lot_id, product_id: 5, customer_id: 7, product_name: "中性内盒",
    specification: "400×300×200", inventory_code: "CODE-5", quantity: 15, unit: "pcs"}));
  const {nodes} = render({locations: [{...locations[0], items}]});
  const cards = nodes.filter(node => node.props.className === "shelf-product-card");
  assert.equal(cards.length, 1);
  const visibleText = node => node && typeof node === "object" ? node.children.map(visibleText).join(" ") : String(node ?? "");
  const content = visibleText(cards[0]);
  assert.ok(content.indexOf("中性内盒") < content.indexOf("CODE-5"));
  assert.ok(content.indexOf("400×300×200") < content.indexOf("CODE-5"));
  assert.ok(content.includes('30'));
  assert.equal(nodes.filter(node => node.props.className === "shelf-batch-row").length, 2);
  assert.equal(nodes.filter(node => node.props.className === "mold-rack-book-spines").length, 0);
  const codeRow = nodes.find(node => node.props.className === "shelf-product-code-row");
  assert.ok(codeRow, "code and details must share a row with separate controls");
  assert.equal(codeRow.children.filter(node => node?.type === "button").length, 2);
  const summary = nodes.find(node => node.props.className === "shelf-product-summary");
  assert.ok(summary);
  assert.deepEqual(summary.children.map(node => node.props.className), ['shelf-product-customer','shelf-product-name','shelf-specification']);
  assert.equal(codeRow.children[1].props.className, 'shelf-product-quantity');
  const details = nodes.find(node => node.props.className === 'shelf-product-details');
  assert.ok(!visibleText(details).includes('中性内盒'));
  assert.ok(!visibleText(details).includes('400×300×200'));
  assert.ok(visibleText(details).includes('首次入库'));
  const heading = nodes.find(node => node.props.className === 'shelf-cell-heading' && node.children.some(child => child?.props?.className === 'shelf-cell-kind'));
  assert.ok(heading, 'single/mixed summary must be in cell heading');
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
      assert.ok(actions.some(([name, value]) => name === "setLocationDetailOpen" && value === false));
    } else assert.deepEqual(actions, []);
  }
});

test("code opens its product label while details only toggles that product's batches", () => {
  const original = sandbox.useState;
  const states = []; let index = 0;
  sandbox.useState = initial => {
    const slot = index++;
    if (!(slot in states)) states[slot] = initial;
    return [states[slot], value => { states[slot] = typeof value === 'function' ? value(states[slot]) : value; }];
  };
  const product = {lot_id: 71, product_id: 5, customer_id: 7, inventory_code: 'CODE-5', unit: 'pcs'};
  const overrides = {locations: [{...locations[0], items: [product]}]};
  const draw = () => { index = 0; return render(overrides).nodes; };
  try {
    let nodes = draw();
    const toggle = nodes.find(n => n.props.className === 'shelf-product-details-toggle');
    assert.equal(toggle.props['aria-expanded'], false);
    toggle.props.onClick();
    assert.equal(states[0], null, 'opening details must not select a product label');
    nodes = draw();
    assert.equal(nodes.find(n => n.props.className === 'shelf-product-details').props.hidden, false);
    nodes.find(n => n.props.className?.includes('shelf-product-label-button')).props.onClick();
    assert.equal(states[0], product);
    assert.equal(states[1], false);
    nodes = draw();
    assert.equal(nodes.find(n => n.props.className === 'shelf-product-details-toggle').props['aria-expanded'], true, 'label click does not collapse details');
    nodes.find(n => n.props.className === 'shelf-product-details-toggle').props.onClick();
    assert.equal(draw().find(n => n.props.className === 'shelf-product-details').props.hidden, true);
  } finally { sandbox.useState = original; }
});

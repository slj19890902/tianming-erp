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
  position_status: 'mapped',
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
  const selectedLots = [];
  const tree = sandbox.WarehouseRackElevation({rack, locations, canChooseProducts: true,
    rackIndex: 0, rackCount: 1, unboundLocationCount: 0,
    onPrevious() {}, onNext() {}, onClose() {}, onRefocus() {}, onSelectLocation: id => inspected.push(id),
    onSelectLot: (locationId, lotId) => selectedLots.push([locationId, lotId]), onChooseEmptyLocation: id => selected.push(id), ...overrides});
  function nodes(node) { return node && typeof node === "object" ? [node, ...node.children.flatMap(nodes)] : []; }
  const emptyControls = nodes(tree).filter(node => node.props.className?.includes("mold-rack-empty-spine"));
  return {selected, inspected, selectedLots, emptyControls, nodes: nodes(tree)};
}

test('first rack read and failed read never claim that formal locations are absent', () => {
  for (const state of [{inventoryLoading: true}, {inventoryError: '读取超时'}]) {
    const {nodes, emptyControls} = render({locations: [], ...state});
    const text = nodes.flatMap(node => node.children.filter(child => typeof child === 'string')).join(' ');
    assert.doesNotMatch(text, /未建正式货位|暂无已建空货位/);
    assert.match(text, state.inventoryLoading ? /正在读取货架库存/ : /货架库存读取失败/);
    assert.equal(emptyControls.length, 0);
  }
  let retried = 0;
  const {nodes} = render({locations: [], inventoryError: '读取超时', onRetryInventory: () => retried++});
  nodes.find(node => node.type === 'button' && node.children.includes('重新读取')).props.onClick();
  assert.equal(retried, 1);
});

test('an existing rack cell with invalid map position remains visible and read-only', () => {
  const {nodes, selected, inspected} = render({locations: locations.map(row => ({...row, position_status: 'unlocated'}))});
  const text = nodes.flatMap(node => node.children.filter(child => typeof child === 'string')).join(' ');
  assert.match(text, /货位位置待核对/);
  assert.doesNotMatch(text, /未建正式货位/);
  for (const button of nodes.filter(node => node.props.className === 'mold-rack-empty-spine')) button.props.onClick();
  assert.deepEqual(selected, []);
  assert.equal(inspected.length, 9);
});

test('formal rack identity survives an unmapped position or an unrelated local draft', () => {
  const start = source.indexOf('  const focusedRackLocations =');
  const end = source.indexOf('  const unboundRackLocationCount', start);
  const code = ts.transpileModule(source.slice(start, end), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  const row = {...locations[0], floor_code: '3F', address_kind: 'rack_slot', storage_type: 'rack', is_active: true, position_status: 'unplaced'};
  const rows = [row, {...row, location_id: 102, map_rack_id: 'another'}, {...row, location_id: 103, is_active: false}];
  const context = {useMemo: fn => fn(), dashboard: {locations: rows}, visualLocations: rows, focusedRack: rack,
    floorCode: '3F', locationDrafts: {[row.location_id]: {}}, normalizeInventoryLocationProjection: value => value};
  const found = vm.runInNewContext(code + '\nfocusedRackLocations;', context);
  assert.deepEqual(Array.from(found, item => item.location_id), [101]);
});

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

test('selected batch is controlled by the shared right detail and survives elevation remounts', () => {
  const shared = {product_id: 7, customer_id: 3, inventory_type: 'finished', inventory_code: 'A',
    product_name: '测试纸箱', box_style: '0201', is_bom_component: false, unit: 'pcs', location_id: 101};
  const items=[{...shared,lot_id:71},{...shared,lot_id:72}];
  for (const item of items) {
    const {nodes} = render({locations:[{...locations[0],items}],selectedLotId:item.lot_id});
    const selected = nodes.filter(node => node.props.className?.includes('shelf-product-label-button selected'));
    assert.equal(selected.length, 1);
    assert.ok(selected[0].children.flat(Infinity).some(child => child?.children?.includes(item.inventory_code)));
  }
  assert.doesNotMatch(component, /setSelectedItem|setExpandedProductGroups/);
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

test("cell selection keeps the elevation and focuses the existing inspector without changing mode or drafts", () => {
  assert.match(source, /onSelectLocation=\{selectRackLocation\}/);
  const callback = source.slice(source.indexOf('  const selectRackLocation ='), source.indexOf('  const chooseRackEmptyLocation ='));
  const code = ts.transpileModule(callback, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const mapMode of ['browse', 'move']) for (const moveAction of ['relocate', 'stocktake', 'merge']) {
    for (const blocked of ['none', 'busy', 'missing']) {
      const actions = [];
      const context = {mapMode, moveAction, spatialEditBusy: blocked === 'busy', focusedRackLocations: locations,
        selectOperationalEntity: (value, origin) => actions.push(['select', value.id, origin]),
        inspectorRef: {current: {focus: () => actions.push(['focus']), scrollIntoView: () => actions.push(['scroll'])}},
        requestAnimationFrame: fn => fn(),
        rackFocusId: 'rack-f9', rackFocusTokenRef: {current: 4}, setCameraFocusTarget: value => actions.push(['camera', value.entity.id]),
        setLocationDetailOpen: value => actions.push(['detail', value]),
      };
      vm.runInNewContext(code + `\nselectRackLocation(${blocked === 'missing' ? 999 : 107});`, context);
      assert.deepEqual(actions, blocked === 'none'
        ? [['select', 'erp-location-107', 'rack'], ['camera', 'rack-f9'], ['detail', false], ['focus'], ['scroll']]
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

test("carton cells show a readable summary while every batch remains in the shared right detail", () => {
  const items = [1, 2].map(lot_id => ({lot_id, product_id: 5, customer_id: 7, product_name: "中性内盒",
    inventory_type: 'finished', box_style: '0201', is_bom_component: false, location_id: 101,
    specification: "400×300×200", inventory_code: "CODE-5", quantity: 15, unit: "pcs"}));
  const {nodes} = render({locations: [{...locations[0], items}]});
  const cards = nodes.filter(node => node.props.className === "shelf-product-card");
  assert.equal(cards.length, 1);
  const visibleText = node => node && typeof node === "object" ? node.children.map(visibleText).join(" ") : String(node ?? "");
  const content = visibleText(cards[0]);
  assert.ok(content.indexOf("中性内盒") < content.indexOf("CODE-5"));
  assert.ok(content.indexOf("400×300×200") < content.indexOf("CODE-5"));
  assert.ok(content.includes('30'));
  assert.equal(nodes.filter(node => node.props.className === "shelf-batch-row").length, 0);
  assert.equal(nodes.filter(node => node.props.className === "mold-rack-book-spines").length, 0);
  const codeRow = nodes.find(node => node.props.className === "shelf-product-code-row");
  assert.ok(codeRow, "code and quantity share the readable rack summary row");
  assert.equal(codeRow.children.filter(node => node?.type === "button").length, 1);
  const summary = nodes.find(node => node.props.className === "shelf-product-summary");
  assert.ok(summary);
  assert.deepEqual(summary.children.map(node => node.props.className), ['shelf-product-customer','shelf-product-name','shelf-specification','shelf-product-batches']);
  assert.equal(codeRow.children[1].props.className, 'shelf-product-quantity');
  assert.equal(nodes.filter(node => node.props.className === 'shelf-product-details').length, 0);
  const batchCount = nodes.find(node => node.props.className === 'shelf-product-batches');
  assert.match(visibleText(batchCount), /2\s*批/);
  const heading = nodes.find(node => node.props.className === 'shelf-cell-heading' && node.children.some(child => child?.props?.className === 'shelf-cell-kind'));
  assert.ok(heading, 'single/mixed summary must be in cell heading');
});

test("the selected empty cell opens its stocktake inspector without writing inventory", () => {
  const callback = source.slice(source.indexOf("  const chooseRackEmptyLocation ="), source.indexOf("  const switchWarehouseFloor ="));
  const code = ts.transpileModule(callback, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const allowed of [true, false]) {
    const actions = [];
    const context = {canStocktake: allowed, mapMode: "move", moveSource: null, spatialEditBusy: false,
      focusedRackLocations: locations, dashboardLoading: false, dashboardError: '', locationDrafts: {},
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

test("rack add callback rejects stale reads, unplaced cells and unpublished drafts", () => {
  const callback = source.slice(source.indexOf("  const chooseRackEmptyLocation ="), source.indexOf("  const switchWarehouseFloor ="));
  const code = ts.transpileModule(callback, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  for (const override of [{dashboardLoading:true}, {dashboardError:'读取失败'},
    {focusedRackLocations:locations.map(row => ({...row,position_status:'unplaced'}))},
    {locationDrafts:{107:{}}}, {focusedRackLocations:[]}]) {
    const context = {canStocktake:true, mapMode:'move', moveSource:null, spatialEditBusy:false,
      focusedRackLocations:locations, dashboardLoading:false, dashboardError:'', locationDrafts:{},
      setViewMode() { assert.fail('blocked rack must not open stock entry'); }, ...override};
    vm.runInNewContext(code + '\nchooseRackEmptyLocation(107);',context);
  }
});

test("code opens the unique right detail and cells show two readable products before an explicit view-all action", () => {
  const product = {lot_id: 71, product_id: 5, customer_id: 7, inventory_code: 'CODE-5', unit: 'pcs'};
  const another = {...product, lot_id: 72, product_id: 6, inventory_code: 'CODE-6'};
  const third = {...product, lot_id: 73, product_id: 7, inventory_code: 'CODE-7'};
  const result = render({locations: [{...locations[0], items: [product, another, third]}]});
  const code = result.nodes.find(n => n.props.className?.includes('shelf-product-label-button'));
  code.props.onClick();
  assert.deepEqual(result.selectedLots, [[101, 71]]);
  const all = result.nodes.find(n => n.props.className === 'shelf-view-all-products');
  assert.ok(all);
  assert.match(all.children.join(''), /查看全部 3 款/);
  all.props.onClick();
  assert.deepEqual(result.selectedLots, [[101, 71], [101, 71]]);
  assert.equal(result.nodes.filter(n => n.props.className === 'shelf-product-card').length, 2, 'two summaries remain readable before the whole-rack scroll surface');
});

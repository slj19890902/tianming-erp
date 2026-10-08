import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html = fs.readFileSync(new URL('../../../static/mobile_erp.html', import.meta.url), 'utf8');
const stocktake = fs.readFileSync(new URL('../../../static/mobile_stocktake.html', import.meta.url), 'utf8');
function fn(name, context) {
  let start = html.indexOf(`      function ${name}(`);
  if (start < 0) start = html.indexOf(`      async function ${name}(`);
  const end = html.indexOf('\n      }', start) + 8;
  return vm.runInNewContext(goodsHelpers+`;(${html.slice(start, end)})`, context);
}
const goodsHelpers=html.slice(html.indexOf('      function groupWarehouseRackGoods('),html.indexOf('      function renderWarehouseLocationGoods('));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this.events = {}; this.classList = { add() {} }; this.parentElement = { clientWidth: 360 }; }
  querySelector() { return {scrollTop:0}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  addEventListener(name, action) { this.events[name] = action; }
}
test('mobile inline scripts parse', () => {
  for (const source of [html, stocktake]) for (const match of source.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});

test('pending relocation uses the guarded placement command without inventing source geometry', async () => {
  const state = {warehouseMapSource: {good: {lot_id: 9, lot_version: 3, quantity_movable: 100},
    location: {location_id: 7, is_pending_relocation: true}},
    warehouseMapTarget: {location_id: 8, layout_version: 4, address_version: 2, published_map_revision: 'map-current'}};
  let posted;
  await fn('confirmWarehouseMapMove', {state, byId: () => ({value: '40'}), warehouseRequestKey: () => 'one-key',
    apiPost: async (url, payload) => {posted = {url, payload}; return {};},
    window: {alert: message => {assert.doesNotMatch(message, /身份不完整/);}}, loadWarehouseMapArea: async () => {}})();
  assert.equal(posted.url, '/api/warehouse/twin-operations/pending-lots/9/place');
  assert.equal(posted.payload.quantity, 40);
  assert.equal(posted.payload.expected_address_version, 2);
  assert.equal(posted.payload.expected_map_revision, 'map-current');
});

test('letter grouping uses region letters for floor-level selection', () => {
  const letter = fn('warehouseAreaLetter', {});
  assert.equal(letter({area_name: '南A1'}), 'A');
  assert.equal(letter({area_name: '北Z3'}), 'Z');
  assert.equal(letter({area_name: '新区域（待设置）'}), '其他');
});
test('rack levels appear inside a clickable front elevation, not overlapping map rectangles', () => {
  const elements = new Map();
  const byId = id => { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); };
  let selected;
  const location = { location_id: 4, map_rack_id: 'r1', rack_display_name: '一号货架', level_no: 1, slot_no: 1, can_select_target: true };
  const state = { warehouseMapData: { map_status: 'ready', locations: [location, {...location, location_id: 5, level_no: 2}] }, warehouseMapZoom: 1 };
  fn('renderWarehouseMap', {state, byId, node: tag => new Element(tag), compactWarehouseLocation: () => '格',
    renderWarehouseMap: () => {}, renderWarehouseLocationGoods: row => { selected = row.location_id; }, showStatus() {}, selectWarehouseMapTarget() {}})();
  const racks = byId('warehouseRackList').children;
  assert.equal(racks.length, 1);
  assert.equal(racks[0].tag, 'details');
  assert.equal(racks[0].children.length, 3);
  const cell = racks[0].children[1].children[1].children[0];
  assert.equal(cell.style.left, undefined);
  cell.events.click();
  assert.equal(selected, 5);
  assert.equal(byId('warehouseMapStage').hidden, true);
});
test('return from a location restores its area instead of closing warehouse map', () => {
  const state = {warehouseLocationPage: true, warehouseMapFocusLocationId: 4};
  let restored = 0;
  fn('closeWarehouseMap', {state, showWarehouseArea: () => restored++, byId: () => ({querySelector:()=>({scrollTop:0})})})();
  assert.equal(restored, 1);
  assert.equal(state.warehouseMapFocusLocationId, null);
  assert.match(stocktake, /return_floor/);
  assert.match(stocktake, /\+'#warehouse'/);
  assert.match(stocktake, /stopImmediatePropagation/);
});

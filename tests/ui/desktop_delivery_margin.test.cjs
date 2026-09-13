const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const rootPath = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(rootPath, "static/ui/desktop-delivery-margin.js"), "utf8");
const html = fs.readFileSync(path.join(rootPath, "static/index.html"), "utf8");

test("desktop margin module keeps contract markers and does not borrow report pages as customer directory", () => {
  assert.match(html, /desktop-delivery-margin\.css\?v=20260913-desktopmargin001/);
  assert.match(html, /desktop-delivery-margin\.js\?v=20260913-desktopmargin001/);
  assert.match(html, /id="desktopDeliveryMarginRoot"/);
  assert.match(html, /canViewDeliveryMargin/);
  assert.match(html, /回单确认销售额（原口径）/);
  assert.match(html, /对账毛利（原口径）/);
  assert.doesNotMatch(html, /management_summary_allowed[^\n]*delivery_margin/);
  assert.match(source, /customer-delivery-margin/);
  assert.match(source, /master\/customers/);
  assert.match(source, /page_size.*50/);
  assert.match(source, /delivery_margin_allowed/);
  assert.match(source, /AbortController/);
  assert.match(source, /generation/);
  assert.match(source, /材料毛利/);
  assert.match(source, /覆盖率/);
  assert.match(source, /actual_material_cost/);
  assert.match(source, /supplemental_material_cost/);
  assert.match(source, /gapSummary/);
  assert.match(source, /bar\.hidden/);
  assert.doesNotMatch(source, /Math\.max\(3/);
  assert.match(html, /不是净利/);
});

test("desktop margin permission, lifecycle, stale response and customer search fixture", async () => {
  class FakeElement {
    constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.hidden = false; this.value = ""; this.disabled = false; this.textContent = ""; this.style = {}; this.attributes = {}; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
    removeEventListener(name, handler) { this.listeners[name] = (this.listeners[name] || []).filter(item => item !== handler); }
    querySelector(selector) { return this._map?.[selector.replace(/^#/, "")] || null; }
  }
  const ids = ["desktopDeliveryMarginState", "desktopDeliveryMarginSummary", "desktopDeliveryMarginBarChart", "desktopDeliveryMarginTrend", "desktopDeliveryMarginCustomers", "desktopDeliveryMarginGaps", "desktopDeliveryMarginCustomer", "desktopDeliveryMarginCustomerSearch", "desktopDeliveryMarginCustomerNote", "desktopDeliveryMarginDateFrom", "desktopDeliveryMarginDateTo", "desktopDeliveryMarginApply", "desktopDeliveryMarginRetry", "desktopDeliveryMarginPrev", "desktopDeliveryMarginNext", "desktopDeliveryMarginPage"];
  const elements = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  const section = new FakeElement("section");
  const root = new FakeElement("div"); root._map = elements;
  const textOf = node => [node.textContent, ...node.children.map(textOf)].join(" ");
  global.document = {createElement(tag) { return new FakeElement(tag); }, getElementById(id) { return id === "desktopDeliveryMarginRoot" ? root : elements[id]; }};
  const requests = [];
  const optionsUrls = [];
  const pending = [];
  const apiGet = (url, config = {}) => {
    if (url.startsWith("/api/master/customers")) {
      optionsUrls.push(config.params || {});
      return Promise.resolve({items: [{id: 99, name: "远端客户"}]});
    }
    requests.push({url, config});
    return new Promise(resolve => pending.push({resolve, config}));
  };
  delete global.window;
  const modulePath = path.join(rootPath, "static/ui/desktop-delivery-margin.js");
  delete require.cache[require.resolve(modulePath)];
  const margin = require(modulePath);
  assert.equal(margin.marginRate({sales_amount: "0.00", material_margin_rate: "0.2"}), "—");
  assert.equal(margin.mount({root, shell: {delivery_margin_allowed: false}, apiGet}), null);
  assert.equal(requests.length, 0);
  const controller = margin.mount({root, shell: {delivery_margin_allowed: true}, customerOptions: [{id: 1, name: "已有授权客户"}], apiGet, isAllowed: () => true});
  assert.ok(controller);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 1);
  elements.desktopDeliveryMarginDateFrom.value = "2026-09-02";
  elements.desktopDeliveryMarginDateTo.value = "2026-09-13";
  elements.desktopDeliveryMarginCustomer.value = "1";
  elements.desktopDeliveryMarginApply.listeners.click[0]();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].config.signal.aborted, true);
  elements.desktopDeliveryMarginCustomerSearch.value = "远端";
  elements.desktopDeliveryMarginCustomerSearch.listeners.input[0]();
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(optionsUrls.at(-1).keyword, "远端");
  elements.desktopDeliveryMarginCustomer.value = "99";
  controller.state.filters.customerId = "99";
  controller.state.page = 3;
  controller.leave();
  assert.equal(requests[1].config.signal.aborted, true);
  const oldRequest = pending[1];
  oldRequest.resolve({summary: {sales_amount: "999.00"}, customers: {items: [], total: 0, page: 1, page_size: 25}, daily: [], gaps: {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.desktopDeliveryMarginSummary.children.length, 0, "stale response does not repaint after leave");
  assert.equal(elements.desktopDeliveryMarginApply.listeners.click.length, 0);
  const elements2 = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  const root2 = new FakeElement("div"); root2._map = elements2;
  const fresh = margin.mount({root:root2, identityKey:"desktop-margin-default", shell: {delivery_margin_allowed: true}, customerOptions: [], apiGet, isAllowed: () => true});
  assert.ok(fresh);
  assert.equal(fresh.state.filters.dateFrom, "2026-09-02", "new DOM remount preserves the user's date filter");
  assert.equal(fresh.state.filters.customerId, "99", "new DOM remount preserves the selected customer");
  assert.equal(fresh.state.page, 3, "new DOM remount preserves the customer page");
  assert.equal(elements2.desktopDeliveryMarginCustomer.value, "99", "new DOM remount restores the selected customer option");
  assert.equal(elements2.desktopDeliveryMarginApply.listeners.click.length, 1, "remount has one apply handler");
  const oldRequests = requests.length;
  elements2.desktopDeliveryMarginApply.listeners.click[0]();
  assert.equal(requests.length, oldRequests + 1);
  pending.slice(2).forEach(item => item.resolve({summary: {status: "partial", delivery_line_count: 1, sales_amount: null, known_sales_amount: "0.00", sales_gap_lines: 1, actual_material_cost: "12.00", supplemental_material_cost: "3.00", material_cost: null, known_material_cost: "15.00", actual_cost_gap_lines: 1, management_cost_gap_lines: 1, material_margin: null, material_margin_rate: null, coverage_rate: "0.0000"}, customers: {items: [{customer_name: "甲客户", metrics: {delivery_line_count: 1, sales_amount: null, known_sales_amount: "0.00", actual_material_cost: "12.00", supplemental_material_cost: "3.00", material_cost: null, known_material_cost: "15.00", sales_gap_lines: 1, actual_cost_gap_lines: 1, management_cost_gap_lines: 1, material_margin: null, material_margin_rate: null, coverage_rate: "0.0000"}}], total: 1, page: 1, page_size: 25}, daily: [], gaps: {total_lines: 1, examples: [{delivery_number: "D-1", delivery_item_id: 7, reason: "售价缺口"}]}}));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(textOf(elements2.desktopDeliveryMarginSummary), /实际：¥12\.00/);
  assert.match(textOf(elements2.desktopDeliveryMarginSummary), /参考补充：¥3\.00/);
  assert.match(textOf(elements2.desktopDeliveryMarginCustomers), /销售缺口 1 行；实际成本缺口 1 行；管理成本缺口 1 行/);
  fresh.destroy();
  delete global.document;
});

test("desktop sync leaves same identity for DOM remount and destroys only on identity change", () => {
  function extractMethod(name) {
    const marker = `${name}() {`;
    const start = html.indexOf(marker);
    assert.ok(start >= 0, `missing ${name}`);
    const bodyStart = start + marker.length - 1;
    let depth = 0;
    for (let index = bodyStart; index < html.length; index += 1) {
      if (html[index] === "{") depth += 1;
      if (html[index] === "}" && --depth === 0) return html.slice(start, index + 1);
    }
    throw new Error(`unterminated ${name}`);
  }
  const syncDesktopDeliveryMargin = Function(`return ({${extractMethod("syncDesktopDeliveryMargin")}}).syncDesktopDeliveryMargin;`)();
  const root = {};
  const module = {
    _identityKey: "7:1", _mountedRoot: null, _savedIdentityKey: null, leaveCount: 0, destroyCount: 0, mountCount: 0,
    leave() { if (this._identityKey) this.leaveCount += 1; this._identityKey = null; this._mountedRoot = null; this._savedIdentityKey = "7:1"; },
    destroy() { this.destroyCount += 1; this._identityKey = null; this._mountedRoot = null; this._savedIdentityKey = null; },
    mount(args) { this.mountCount += 1; this._identityKey = args.identityKey; this._mountedRoot = args.root; },
    updateCustomerOptions() {},
  };
  const previousWindow = global.window;
  const previousDocument = global.document;
  global.window = {TmDesktopDeliveryMargin: module};
  global.document = {getElementById(id) { return id === "desktopDeliveryMarginRoot" ? root : null; }};
  const vm = {activePage: "customers", user: {id: 7}, authGeneration: 1, canViewDeliveryMargin: true, customerOptions: [], $nextTick(callback) { callback(); }};
  syncDesktopDeliveryMargin.call(vm);
  assert.equal(module.leaveCount, 1);
  assert.equal(module.destroyCount, 0);
  vm.activePage = "customers";
  syncDesktopDeliveryMargin.call(vm);
  assert.equal(module.leaveCount, 1, "repeated non-dashboard sync does not re-run a disposed leave");
  vm.activePage = "dashboard";
  syncDesktopDeliveryMargin.call(vm);
  assert.equal(module.mountCount, 1, "same identity remounts the new dashboard DOM");
  assert.equal(module.destroyCount, 0, "returning to dashboard does not destroy saved state");
  syncDesktopDeliveryMargin.call(vm);
  assert.equal(module.mountCount, 1, "same root does not duplicate mount");
  vm.user = {id: 8};
  syncDesktopDeliveryMargin.call(vm);
  assert.equal(module.destroyCount, 1, "identity change destroys the old sensitive state");
  global.window = previousWindow;
  global.document = previousDocument;
});

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "static/mobile_erp.html"), "utf8");
const source = fs.readFileSync(path.join(root, "static/ui/mobile-delivery-margin.js"), "utf8");
const margin = require(path.join(root, "static/ui/mobile-delivery-margin.js"));

test("mobile home wires the isolated delivery margin assets and section", () => {
  assert.equal(fs.existsSync(path.join(root, "static/ui/mobile-delivery-margin.css")), true);
  assert.equal(fs.existsSync(path.join(root, "static/ui/mobile-delivery-margin.js")), true);
  assert.match(html, /\/static\/ui\/mobile-delivery-margin\.css\?v=20260913-mobilemargin001/);
  assert.match(html, /\/static\/ui\/mobile-delivery-margin\.js\?v=20260913-mobilemargin001/);
  assert.match(html, /id="deliveryMarginSection"/);
  assert.match(html, /state\.shell\?\.delivery_margin_allowed === true/);
  assert.match(html, /TmMobileDeliveryMargin\?\.mount/);
  assert.match(html, /不是净利/);
  assert.match(source, /待核对/);
  assert.match(source, /含参考补充/);
  assert.match(source, /无送货/);
  assert.match(source, /coverage_rate/);
  assert.doesNotMatch(html, /management_summary_allowed === true[^\n]*delivery_margin/);
});

test("query preserves the contract filters and paged customer request", () => {
  const query = margin.queryString({customerId: "42", dateFrom: "2026-09-01", dateTo: "2026-09-13"}, 3);
  assert.equal(query, "/api/dashboard/customer-delivery-margin?page=3&page_size=20&customer_id=42&date_from=2026-09-01&date_to=2026-09-13");
});

test("unknown money and rate values stay visibly unknown", () => {
  assert.equal(margin.money(null), null);
  assert.equal(margin.money(""), null);
  assert.equal(margin.rate(null), null);
  assert.equal(margin.rate("0.125"), "12.50%");
});

test("requests are abortable and stale pages cannot paint", () => {
  assert.match(source, /new AbortController\(\)/);
  assert.match(source, /localState\.controller\?\.abort\(\)/);
  assert.match(source, /generation !== localState\.generation/);
  assert.match(source, /onPageChange\(page\)/);
  assert.match(html, /TmMobileDeliveryMargin\?\.onPageChange\?\.\(page\)/);
  assert.match(source, /api\/master\/customers\?page=1&page_size=200&include_inactive=true/);
  assert.match(source, /材料成本毛利/);
  assert.match(source, /送货数量/);
  assert.match(source, /rate\(metrics\.coverage_rate\)/);
  assert.match(source, /delivery_number/);
  assert.match(source, /function destroy\(\)/);
});

test("default dates are Beijing month start through Beijing today", () => {
  const dates = margin.defaultDates();
  assert.match(dates.from, /^\d{4}-\d{2}-01$/);
  assert.match(dates.to, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(dates.from.slice(0, 7), dates.to.slice(0, 7));
});

test("permission and lifecycle guards work with an anonymous DOM/fetch fixture", async () => {
  class FakeElement {
    constructor() {
      this.children = [];
      this.listeners = {};
      this.style = {};
      this.hidden = false;
      this.value = "";
      this.disabled = false;
      this.textContent = "";
    }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this[name] = String(value); }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
    get firstChild() { return this.children[0] || null; }
  }
  const ids = [
    "deliveryMarginSection", "deliveryMarginPanel", "deliveryMarginState", "deliveryMarginSummary",
    "deliveryMarginBarChart", "deliveryMarginTrend", "deliveryMarginCustomers", "deliveryMarginGaps",
    "deliveryMarginCustomer", "deliveryMarginDateFrom", "deliveryMarginDateTo", "loadDeliveryMargin",
    "applyDeliveryMargin", "deliveryMarginPrev", "deliveryMarginNext", "deliveryMarginPageText",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  global.document = {
    getElementById(id) { return elements[id] || (elements[id] = new FakeElement()); },
    createElement() { return new FakeElement(); },
  };
  let called = false;
  assert.equal(margin.mount({shell: {delivery_margin_allowed: false}, apiGet: () => { called = true; }}), null);
  assert.equal(called, false, "employees must not request the cost endpoint");

  const pending = [];
  const requests = [];
  const apiGet = (url, options) => {
    requests.push({url, options});
    if (url.startsWith("/api/master/customers")) return Promise.resolve({items: []});
    return new Promise(resolve => pending.push({resolve, options}));
  };
  const payload = total => ({
    summary: {status: "partial", delivery_line_count: 1, sales_amount: null, known_sales_amount: "0.00", sales_gap_lines: 1, material_cost: null, known_material_cost: "0.00", management_cost_gap_lines: 1, material_margin: null, material_margin_rate: null, coverage_rate: "0.0000"},
    customers: {items: [], page: 1, page_size: 20, total},
    daily: [],
    gaps: {total_lines: 1, reason_counts: {missing_sales_price: 1}, examples: [], examples_truncated: false},
  });
  margin.mount({shell: {delivery_margin_allowed: true}, apiGet});
  assert.equal(requests.filter(request => request.url.startsWith("/api/dashboard/customer-delivery-margin")).length, 1);
  elements.deliveryMarginCustomer.value = "42";
  elements.deliveryMarginDateFrom.value = "2026-09-02";
  elements.deliveryMarginDateTo.value = "2026-09-13";
  elements.applyDeliveryMargin.listeners.click[0]();
  const marginRequests = () => requests.filter(request => request.url.startsWith("/api/dashboard/customer-delivery-margin"));
  assert.equal(marginRequests().length, 2);
  assert.equal(marginRequests()[0].options.signal.aborted, true, "a new filter aborts the prior request");
  elements.deliveryMarginSection.hidden = true;
  margin.onPageChange("lookup");
  assert.equal(marginRequests()[1].options.signal.aborted, true, "leaving home aborts the active request");
  margin.onPageChange("home");
  assert.equal(marginRequests().length, 3, "returning home reloads an interrupted request");
  assert.match(marginRequests()[2].url, /customer_id=42/);
  assert.match(marginRequests()[2].url, /date_from=2026-09-02/);
  assert.match(marginRequests()[2].url, /date_to=2026-09-13/);
  pending[2].resolve(payload(40));
  await new Promise(resolve => setImmediate(resolve));
  pending[1].resolve(payload(0));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.deliveryMarginPageText.textContent, "第 1 / 2 页", "stale response cannot overwrite the current page");
  margin.mount({shell: {delivery_margin_allowed: false}, apiGet});
  assert.equal(elements.deliveryMarginSection.hidden, true, "revoked capability hides the module");
  assert.equal(elements.deliveryMarginSummary.children.length, 0, "revoked capability clears old amounts");
  assert.doesNotMatch(source, /panel\.hidden = true/);
  delete global.document;
});

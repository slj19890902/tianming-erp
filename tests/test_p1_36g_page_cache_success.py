from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the page-cache regression"
    target = tmp_path / "page-cache-success.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_page_cache_is_written_only_after_required_loaders_succeed() -> None:
    page = _method_body(
        "async loadPage(page, { force=false, supplierPromise=null } = {}) {",
        "refreshCurrent() {",
    )
    search = _method_body("queuePageSearch(page) {", "async go(page) {")

    assert "if (requestIsCurrent()) this.invalidatePageCache(page);" in page
    assert "if (await promise === false) pageSucceeded = false;" in page
    assert "if (requestIsCurrent() && pageSucceeded) this.markPageCache(page);" in page
    assert "return requestIsCurrent() && pageSucceeded;" in page
    assert "return true;" in page.split("if (!force && this.pageCacheFresh(page))", 1)[1]
    assert "if (result !== false && currentCachePage === cachePage)" in search
    assert "this.markPageCache(" in search


def test_top_level_loaders_expose_stable_success_and_failure_results() -> None:
    overview = _method_body("async loadOverview() {", "async loadWarehouseCapacitySummary() {")
    incoming = _method_body("async loadIncoming() {", "externalIncomingDraftKey(")
    pickers = _method_body("async loadDeliveryPickers() {", "async assignDeliveryPickTask(")
    finance = _method_body("async loadFinance() {", "financeGroupKey(")
    audit = _method_body("async loadAuditLogs({ resetPage=false } = {}) {", "queryAuditLogs() {")
    permissions = _method_body("async loadPermissionUsers() {", "async loadPermissionProfile(")
    system = _method_body("async loadSystemSection(section, {force=false}={}) {", "async loadSystemVersionHistory(")
    order_customers = _method_body("async loadOrderCustomerOptions() {", "async createCustomerQuotePreference(")

    for block in (overview, pickers, finance, audit, permissions, system, order_customers):
        assert "return true;" in block
        assert "return false;" in block
    assert "return false;" in incoming
    assert "return externalLoaded !== false;" in incoming
    assert "if (!this.canDelivery) return true;" in pickers
    assert 'if (!this.hasPermission("audit.view")) return true;' in audit
    assert "if (!force && this.systemSectionLoaded[section]) return true;" in system


def test_load_page_failure_does_not_cache_and_success_or_empty_result_does(
    tmp_path: Path,
) -> None:
    page_body = _method_body(
        "async loadPage(page, { force=false, supplierPromise=null } = {}) {",
        "refreshCurrent() {",
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
const pageBody = {json.dumps(page_body, ensure_ascii=False)};

function makeVm(loader) {{
  const vm = {{
    pageLoadSequence: 0,
    authGeneration: 0,
    user: {{id: 1}},
    loading: false,
    marks: [],
    invalidations: [],
    toasts: [],
    pageCacheFresh() {{ return false; }},
    invalidatePageCache(page) {{ this.invalidations.push(page); }},
    markPageCache(page) {{ this.marks.push(page); }},
    showToast(message) {{ this.toasts.push(String(message)); }},
    errorMessage(error) {{ return String(error?.message || error); }},
    loadOverview: loader,
  }};
  vm.loadPage = new AsyncFunction(
    "page", "{{ force=false, supplierPromise=null }} = {{}}", pageBody
  ).bind(vm);
  return vm;
}}

(async () => {{
  const failed = makeVm(async () => false);
  expect(await failed.loadPage("dashboard") === false, "false loader was accepted");
  expect(failed.marks.length === 0, "failed page was cached");
  expect(failed.invalidations.join(",") === "dashboard", "old cache was not cleared before retry");
  expect(failed.loading === false, "failed current request left loading active");

  const legacySuccess = makeVm(async () => undefined);
  expect(await legacySuccess.loadPage("dashboard") === true, "legacy undefined success was rejected");
  expect(legacySuccess.marks.join(",") === "dashboard", "successful page was not cached once");

  const thrown = makeVm(async () => {{ throw new Error("dashboard unavailable"); }});
  expect(await thrown.loadPage("dashboard") === false, "exceptional page reported success");
  expect(thrown.marks.length === 0, "exceptional page was cached");
  expect(thrown.toasts.length === 1, "exception did not keep the existing error notification");

  const cached = makeVm(async () => {{ throw new Error("cache hit still loaded"); }});
  cached.pageCacheFresh = () => true;
  expect(await cached.loadPage("dashboard") === true, "fresh cache did not report success");
  expect(cached.invalidations.length === 0 && cached.marks.length === 0, "fresh cache was rewritten");

  const multi = makeVm(async () => true);
  multi.loadCustomerOptions = async () => true;
  multi.loadOrderCustomerOptions = async () => false;
  multi.loadOrders = async () => true;
  expect(await multi.loadPage("orders") === false, "partial multi-loader failure was accepted");
  expect(multi.marks.length === 0, "partially failed page was cached");

  const allGood = makeVm(async () => true);
  allGood.loadCustomerOptions = async () => true;
  allGood.loadOrderCustomerOptions = async () => true;
  allGood.loadOrders = async () => true;
  expect(await allGood.loadPage("orders") === true, "successful multi-loader page failed");
  expect(allGood.marks.join(",") === "orders", "successful multi-loader page was not cached once");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)


def test_debounced_search_does_not_cache_failed_or_replaced_results(tmp_path: Path) -> None:
    search_body = _method_body("queuePageSearch(page) {", "async go(page) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
const searchBody = {json.dumps(search_body, ensure_ascii=False)};
globalThis.searchDebounceTimers = new Map();
let scheduled = null;
globalThis.setTimeout = callback => {{ scheduled = callback; return 1; }};
globalThis.clearTimeout = () => {{}};

(async () => {{
  let result = false;
  const vm = {{
    activePage: "customers",
    pages: {{customers: 2}},
    marks: [], invalidations: [], toasts: [],
    invalidatePageCache(page) {{ this.invalidations.push(page); }},
    markPageCache(page) {{ this.marks.push(page); }},
    loadCustomers: async () => result,
    loadProducts: async () => true,
    loadOrders: async () => true,
    isCancelledRequest: () => false,
    errorMessage: error => String(error?.message || error),
    showToast(message) {{ this.toasts.push(message); }},
  }};
  vm.queuePageSearch = new AsyncFunction("page", searchBody).bind(vm);

  vm.queuePageSearch("customers");
  await scheduled();
  expect(vm.marks.length === 0, "failed search was cached");
  expect(vm.invalidations.join(",") === "customers", "search did not invalidate old cache");

  result = true;
  vm.queuePageSearch("customers");
  await scheduled();
  expect(vm.marks.join(",") === "customers", "successful search was not cached");

  result = true;
  vm.activePage = "orders";
  vm.queuePageSearch("customers");
  await scheduled();
  expect(vm.marks.length === 1, "inactive page search wrote a cache timestamp");

  vm.activePage = "orders_legacy";
  vm.pages.orders = 2;
  vm.invalidations = [];
  result = false;
  vm.loadOrders = async () => result;
  vm.queuePageSearch("orders");
  await scheduled();
  expect(vm.invalidations.join(",") === "orders_legacy", "legacy search cleared the wrong cache key");
  expect(vm.marks.length === 1, "failed legacy search restored its cache timestamp");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)

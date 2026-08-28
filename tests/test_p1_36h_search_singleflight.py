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
    assert node is not None, "Node.js is required for the search single-flight regression"
    target = tmp_path / "search-singleflight.js"
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


def test_high_frequency_explicit_actions_share_one_search_gate() -> None:
    cancel = _method_body("cancelQueuedPageSearch(page) {", "pageSearchCacheKey(page) {")
    explicit = _method_body(
        "async runExplicitPageListLoad(page, {resetPage=false, load=null} = {}) {",
        "queuePageSearch(page) {",
    )
    assert "clearTimeout(pending)" in cancel
    assert "searchDebounceTimers.delete(key)" in cancel
    reset = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")
    assert "pageSearchGenerations.clear();" in reset
    assert explicit.index("this.cancelQueuedPageSearch(page)") < explicit.index(
        "this.invalidatePageCache(cachePage)"
    )
    assert "this.invalidatePageCache(cachePage)" in explicit
    assert "result.every(item => item !== false)" in explicit
    assert "this.advancePageSearchGeneration(page)" in explicit
    assert "this.pageSearchIsCurrent(page, cachePage, authGeneration, userId, generation, context)" in explicit
    assert "this.markPageCache(cachePage)" in explicit
    assert 'runExplicitPageListLoad("orders", {resetPage:true})' in INDEX
    assert "runExplicitPageListLoad('customers',{resetPage:true})" in INDEX
    assert "runExplicitPageListLoad('products',{resetPage:true})" in INDEX
    assert "@keyup.enter=\"pages.customers=1; loadCustomers()\"" not in INDEX
    assert "@keyup.enter=\"pages.products=1; loadProducts()\"" not in INDEX
    assert "@keyup.enter=\"pages.orders=1; loadOrders()\"" not in INDEX


def test_direct_search_cancels_debounce_and_page_two_does_not_jump_back(
    tmp_path: Path,
) -> None:
    cancel_body = _method_body("cancelQueuedPageSearch(page) {", "pageSearchCacheKey(page) {")
    cache_key_body = _method_body("pageSearchCacheKey(page) {", "pageSearchGenerationKey(page) {")
    generation_key_body = _method_body(
        "pageSearchGenerationKey(page) {", "currentPageSearchGeneration(page) {"
    )
    current_generation_body = _method_body(
        "currentPageSearchGeneration(page) {", "advancePageSearchGeneration(page) {"
    )
    advance_generation_body = _method_body(
        "advancePageSearchGeneration(page) {", "pageSearchIsCurrent("
    )
    current_body = _method_body(
        "pageSearchIsCurrent(page, cachePage, authGeneration, userId, generation, context={}) {",
        "async runExplicitPageListLoad(",
    )
    explicit_body = _method_body(
        "async runExplicitPageListLoad(page, {resetPage=false, load=null} = {}) {",
        "queuePageSearch(page) {",
    )
    search_body = _method_body("queuePageSearch(page) {", "async go(page) {")
    go_body = _method_body("async go(page) {", "async openBusinessFlowStep(")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor = Function;
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
const cancelBody = {json.dumps(cancel_body, ensure_ascii=False)};
const cacheKeyBody = {json.dumps(cache_key_body, ensure_ascii=False)};
const generationKeyBody = {json.dumps(generation_key_body, ensure_ascii=False)};
const currentGenerationBody = {json.dumps(current_generation_body, ensure_ascii=False)};
const advanceGenerationBody = {json.dumps(advance_generation_body, ensure_ascii=False)};
const currentBody = {json.dumps(current_body, ensure_ascii=False)};
const explicitBody = {json.dumps(explicit_body, ensure_ascii=False)};
const searchBody = {json.dumps(search_body, ensure_ascii=False)};
const goBody = {json.dumps(go_body, ensure_ascii=False)};

globalThis.searchDebounceTimers = new Map();
globalThis.pageSearchGenerations = new Map();
let nextTimerId = 1;
const timers = new Map();
globalThis.setTimeout = callback => {{
  const id = nextTimerId++;
  timers.set(id, {{callback, cancelled:false}});
  return id;
}};
globalThis.clearTimeout = id => {{
  const timer = timers.get(id);
  if (timer) timer.cancelled = true;
}};
async function drainTimers() {{
  for (const [id, timer] of [...timers.entries()]) {{
    timers.delete(id);
    if (!timer.cancelled) await timer.callback();
  }}
}}

(async () => {{
  const calls = {{customers:[], products:[], orders:[]}};
  const outcomes = {{customers:true, products:true, orders:true}};
  const vm = {{
    activePage: "customers",
    authGeneration: 0,
    user: {{id:1}},
    pages: {{customers:3, products:4, orders:5}},
    marks: [], invalidations: [], toasts: [],
    invalidatePageCache(page) {{ this.invalidations.push(page); }},
    markPageCache(page) {{ this.marks.push(page); }},
    isCancelledRequest: () => false,
    errorMessage: error => String(error?.message || error),
    showToast(message) {{ this.toasts.push(String(message)); }},
  }};
  vm.cancelQueuedPageSearch = new FunctionCtor("page", cancelBody).bind(vm);
  vm.pageSearchCacheKey = new FunctionCtor("page", cacheKeyBody).bind(vm);
  vm.pageSearchGenerationKey = new FunctionCtor("page", generationKeyBody).bind(vm);
  vm.currentPageSearchGeneration = new FunctionCtor("page", currentGenerationBody).bind(vm);
  vm.advancePageSearchGeneration = new FunctionCtor("page", advanceGenerationBody).bind(vm);
  vm.pageSearchIsCurrent = new FunctionCtor(
    "page", "cachePage", "authGeneration", "userId", "generation", "context={{}}", currentBody
  ).bind(vm);
  vm.runExplicitPageListLoad = new AsyncFunction(
    "page", "{{resetPage=false, load=null}}={{}}", explicitBody
  ).bind(vm);
  vm.queuePageSearch = new AsyncFunction("page", searchBody).bind(vm);
  vm.pageAllowed = () => true;
  vm.cancelSupplierRequisitionPreview = () => {{}};
  vm.loadPage = async () => true;
  vm.go = new AsyncFunction("page", goBody).bind(vm);
  vm.loadCustomers = async function() {{
    calls.customers.push(this.pages.customers);
    return outcomes.customers;
  }};
  vm.loadProducts = async function() {{
    calls.products.push(this.pages.products);
    return outcomes.products;
  }};
  vm.loadCurrentOrderWorkspace = async function() {{
    calls.orders.push(this.pages.orders);
    return outcomes.orders;
  }};

  vm.queuePageSearch("customers");
  await vm.runExplicitPageListLoad("customers");
  await drainTimers();
  expect(calls.customers.join(",") === "3", "customer Enter issued a second request");
  expect(vm.invalidations.length >= 1 && vm.invalidations.every(page => page === "customers"), "customer query cleared the wrong cache");
  expect(vm.marks.join(",") === "customers", "customer query did not mark one fresh cache");

  vm.activePage = "products";
  vm.queuePageSearch("products");
  await vm.runExplicitPageListLoad("products");
  await drainTimers();
  expect(calls.products.join(",") === "4", "product query issued a second request");

  vm.activePage = "orders";
  vm.queuePageSearch("orders");
  vm.pages.orders = 2;
  await vm.runExplicitPageListLoad("orders");
  await drainTimers();
  expect(calls.orders.join(",") === "2", "order page change issued a delayed page-one request");
  expect(vm.pages.orders === 2, "delayed debounce moved the order list back to page one");

  calls.orders = [];
  vm.pages.orders = 6;
  vm.queuePageSearch("orders");
  vm.queuePageSearch("orders");
  await drainTimers();
  expect(calls.orders.join(",") === "1", "continuous typing did not collapse to one request");
  expect(vm.pages.orders === 1, "normal debounce did not reset the result page");

  calls.orders = [];
  vm.marks = [];
  vm.invalidations = [];
  vm.activePage = "orders_legacy";
  vm.pages.orders = 3;
  vm.queuePageSearch("orders");
  await drainTimers();
  expect(calls.orders.join(",") === "1", "legacy order debounce did not issue one request");
  expect(vm.invalidations.length >= 1 && vm.invalidations.every(page => page === "orders_legacy"), "legacy debounce cleared the wrong cache");
  expect(vm.marks.join(",") === "orders_legacy", "legacy debounce marked the wrong cache");

  outcomes.orders = false;
  vm.marks = [];
  vm.invalidations = [];
  vm.queuePageSearch("orders");
  await drainTimers();
  expect(vm.invalidations.length >= 1 && vm.invalidations.every(page => page === "orders_legacy"), "failed search did not clear stale cache");
  expect(vm.marks.length === 0, "failed search wrote a fresh cache timestamp");
  outcomes.orders = true;

  vm.activePage = "orders";
  vm.marks = [];
  vm.invalidations = [];
  const compositeFailed = await vm.runExplicitPageListLoad("orders", {{
    load:async() => [true, false],
  }});
  expect(compositeFailed === false && vm.marks.length === 0, "partial filter refresh was cached");
  const compositeSucceeded = await vm.runExplicitPageListLoad("orders", {{
    load:async() => [true, true],
  }});
  expect(compositeSucceeded === true && vm.marks.join(",") === "orders", "complete filter refresh was not cached once");

  vm.marks = [];
  vm.invalidations = [];
  let releaseOldOrder;
  const oldOrder = vm.runExplicitPageListLoad("orders", {{
    load:() => new Promise(resolve => {{ releaseOldOrder = resolve; }}),
  }});
  await Promise.resolve();
  vm.queuePageSearch("orders");
  releaseOldOrder(true);
  expect(await oldOrder === false, "old request survived a newer keyword input");
  expect(vm.marks.length === 0, "old request restored cache after a newer keyword input");
  vm.cancelQueuedPageSearch("orders");

  vm.activePage = "products";
  vm.productTab = "products";
  vm.selectedProductCustomer = {{id:7}};
  vm.marks = [];
  let releaseProduct;
  vm.loadProducts = () => new Promise(resolve => {{ releaseProduct = resolve; }});
  const staleProduct = vm.runExplicitPageListLoad("products");
  await Promise.resolve();
  vm.productTab = "materials";
  releaseProduct(true);
  expect(await staleProduct === false, "old product-tab query was accepted");
  expect(vm.marks.length === 0, "old product-tab query cached the wrong tab");

  vm.activePage = "customers";
  vm.productTab = "products";
  vm.selectedProductCustomer = null;
  const beforeLeaveReturn = calls.customers.length;
  vm.queuePageSearch("customers");
  expect(vm.invalidations.at(-1) === "customers", "new input did not invalidate cache immediately");
  await vm.go("products");
  await vm.go("customers");
  await drainTimers();
  expect(calls.customers.length === beforeLeaveReturn, "leave-and-return executed an old debounce");

  const customerCount = calls.customers.length;
  vm.activePage = "customers";
  vm.queuePageSearch("customers");
  vm.activePage = "products";
  await drainTimers();
  expect(calls.customers.length === customerCount, "inactive page debounce still requested data");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)

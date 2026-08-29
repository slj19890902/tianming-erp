import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_source(start: str, end: str) -> str:
    return INDEX.split(start, 1)[1].split(end, 1)[0]


def _method_body(start: str, end: str) -> str:
    return _method_source(start, end).rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    target = tmp_path / "p1-125-production-history-race.js"
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


def test_order_page_separates_daily_queue_recent_and_conditioned_history() -> None:
    for label in (
        "全部待处理",
        "最近完成（10天）",
        "完成历史查询",
        "最近作废（10天）",
        "作废历史查询",
    ):
        assert label in INDEX
    assert "10 天以前的历史默认不自动加载" in INDEX
    assert "orderHistorySearchReady()" in INDEX


def test_order_loader_uses_exact_inventory_code_and_skips_blocking_badge_count() -> None:
    loader = _method_source("async loadOrders() {", "async autoReleaseReadyRequisitionHolds")
    assert "include_unfinished_total:false" in loader
    assert 'params.product_code_match = "exact"' in loader
    assert 'params.history_mode = historyMode' in loader
    assert 'params.recent_days = 10' in loader
    assert 'scopeValue.startsWith("completed_")' in loader
    assert 'scopeValue.startsWith("cancelled_")' in loader
    assert "data.unfinished_total !== null" in loader


def test_history_search_condition_does_not_treat_scope_as_a_query() -> None:
    source = _method_source("orderHistorySearchReady() {", "filteredRequisitionPending()")
    assert "this.filters.orderScope" not in source
    for field in (
        "orderKeyword",
        "orderCustomer",
        "orderNumber",
        "orderProductCode",
        "orderProductName",
        "orderDateFrom",
        "orderDeliveryDateFrom",
    ):
        assert field in source


def test_completion_history_uses_keyset_cursor_and_exact_inventory_code() -> None:
    loader = _method_source(
        "async loadProductionHistory() {",
        "resetProductionHistoryCursor() {",
    )
    assert 'pagination: "cursor"' in loader
    assert "params.cursor = this.productionHistoryCursor" in loader
    assert 'params.product_code_match = "exact"' in loader
    assert 'this.beginLatestRequest("production:history")' in loader
    assert "signal:controller.signal" in loader
    assert 'latestRequestControllers.get("production:history") !== controller' in loader
    assert "data.has_more" in loader
    assert "data.next_cursor" in loader
    assert "previousProductionHistoryPage()" in INDEX
    assert "nextProductionHistoryPage()" in INDEX
    assert "第 {{ pages.productionHistory }} 页" in INDEX


def test_production_entry_does_not_load_the_hidden_large_history_list() -> None:
    loader = _method_source("async loadProduction() {", "async openProductionLabelMaintenance")
    placement_branch = loader.split('if (this.productionTab === "placement") {', 1)[1].split("const [, historyResult]", 1)[0]
    history_branch = loader.split("const [, historyResult]", 1)[1]
    assert "this.loadProductionPlacement()" in placement_branch
    assert "this.loadProductionHistory()" not in placement_branch
    assert "this.loadProductionHistory()" in history_branch
    assert "this.loadProductionPlacement()" not in history_branch


def test_order_first_page_does_not_wait_for_scope_customer_aggregation() -> None:
    loader = _method_source("async loadPage(page", "refreshCurrent()")
    order_branch = loader.split('if (page === "orders") {', 1)[1].split(
        'if (page === "orders_legacy")', 1
    )[0]
    blocking = order_branch.split("if (requestIsCurrent()", 1)[0]
    assert "this.loadCurrentOrderWorkspace()" in blocking
    assert "this.loadOrderCustomerOptions()" not in blocking
    assert "void this.loadOrderCustomerOptions()" in order_branch


def test_history_customer_selector_uses_master_options_without_scanning_history() -> None:
    loader = _method_source(
        "async loadOrderCustomerOptions() {",
        "async createCustomerQuotePreference()",
    )
    assert 'if (historyMode === "history") {' in loader
    assert "this.orderCustomerOptions = (this.customerOptions || []).map" in loader
    history_branch = loader.split('if (historyMode === "history") {', 1)[1].split(
        "if (historyMode)", 1
    )[0]
    assert 'axios.get("/api/orders/customer-options"' not in history_branch


def test_completion_history_latest_request_wins(tmp_path: Path) -> None:
    body = _method_body(
        "async loadProductionHistory() {",
        "resetProductionHistoryCursor() {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  productionHistoryCursor:"",productionHistoryFilters:{{}},productionHistory:[],productionHistoryTotal:0,
  productionHistoryHasMore:false,productionHistoryNextCursor:"",productionLocations:[],canWarehouseExecute:false,
  productionHistoryPageSize(){{return 1;}},
  beginLatestRequest(key){{const previous=global.latestRequestControllers.get(key);previous?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},showToast(){{}},errorMessage(error){{return String(error);}},
  ensureProductionLocations(){{return Promise.resolve(true);}}
}};
vm.loadProductionHistory=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadProductionHistory();const second=vm.loadProductionHistory();
  pending[0].resolve({{data:{{items:[{{id:1,status:"posted"}}],total:null,has_more:false,next_cursor:null}}}});
  expect(await first===false,"stale completion response was accepted");
  expect(vm.productionHistory.length===0,"stale completion rows replaced current state");
  pending[1].resolve({{data:{{items:[{{id:2,status:"posted"}}],total:null,has_more:true,next_cursor:"next"}}}});
  expect(await second===true,"latest completion response failed");
  expect(vm.productionHistory[0].id===2,"latest completion row was not retained");
  expect(vm.productionHistoryHasMore&&vm.productionHistoryNextCursor==="next","cursor state was not retained");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)

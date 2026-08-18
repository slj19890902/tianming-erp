import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_source(start: str, end: str) -> str:
    return INDEX.split(start, 1)[1].split(end, 1)[0]


def _order_queue_template() -> str:
    return INDEX.split('<template v-else>\n              <div class="order-list-panel">', 1)[1].split(
        '<template v-else-if="activePage === \'orders_legacy\'">', 1
    )[0]


def test_order_queue_has_one_vertical_scroll_path_and_one_authoritative_pager() -> None:
    assert '.order-list-panel .table-wrap {' in INDEX
    order_css = INDEX.split('.order-list-panel .table-wrap {', 1)[1].split('}', 1)[0]
    assert 'max-height: none' in order_css
    assert 'overflow-x: auto' in order_css
    assert 'overflow-y: visible' in order_css
    assert 'overflow: hidden' not in order_css

    queue = _order_queue_template()
    assert 'class="order-list-panel"' in INDEX
    assert queue.count('<pager ') == 0
    order_filter_footer = INDEX.split('<div class="order-filter-footer">', 1)[1][:2400]
    assert ':page-size="orderListPageSize()"' in order_filter_footer
    assert ':order-layout="true"' in order_filter_footer
    order_pager = INDEX.split("<template v-if=\"$attrs['order-layout']\">", 1)[1].split(
        '<template v-else>', 1
    )[0]
    assert order_pager.index('上一页') < order_pager.index('第 {{ page }}/{{ pages }} 页')
    assert order_pager.index('第 {{ page }}/{{ pages }} 页') < order_pager.index('下一页')


def test_order_queue_uses_mode_specific_server_page_sizes() -> None:
    assert 'ORDER_LIST_PAGE_SIZE_STANDARD = 12' in INDEX
    assert 'ORDER_LIST_PAGE_SIZE_LARGE = 7' in INDEX

    sizing = _method_source('orderListPageSize(mode=this.uiMode) {', 'async reloadOrdersForUiModeChange')
    assert 'ORDER_LIST_PAGE_SIZE_LARGE' in sizing
    assert 'ORDER_LIST_PAGE_SIZE_STANDARD' in sizing

    loader = _method_source('async loadOrders() {', 'async autoReleaseReadyRequisitionHolds')
    assert 'this.activePage === "orders" && this.orderWorkspace === "queue"' in loader
    assert '? this.orderListPageSize()' in loader
    assert 'page_size: effectivePageSize' in loader
    assert 'const lastPage = Math.max(1, Math.ceil(total / responsePageSize));' in loader
    assert 'requestedPage > lastPage' in loader
    assert 'this.pages.orders = lastPage' in loader


def test_ui_mode_change_resets_order_page_and_invalidates_only_order_cache() -> None:
    reload_method = _method_source(
        'async reloadOrdersForUiModeChange(previousMode, nextMode) {',
        'hasPermission(code) {'
    )
    assert 'this.pages.orders = 1' in reload_method
    assert 'this.invalidatePageCache("orders")' in reload_method
    assert 'this.activePage === "orders"' in reload_method
    assert 'this.orderWorkspace === "queue"' in reload_method
    assert 'this.runExplicitPageListLoad("orders")' in reload_method

    mode_switch = _method_source('async setUiMode(mode) {', 'async reloadOrdersForUiModeChange')
    assert 'await this.reloadOrdersForUiModeChange(previousMode, this.uiMode)' in mode_switch


def test_existing_order_filters_still_reset_to_first_page_and_latest_request_wins() -> None:
    explicit = _method_source(
        'async runExplicitPageListLoad(page, {resetPage=false, load=null} = {}) {',
        'queuePageSearch(page) {'
    )
    assert 'this.pages[page] = 1' in explicit

    loader = _method_source('async loadOrders() {', 'async autoReleaseReadyRequisitionHolds')
    assert 'this.beginLatestRequest("orders:list")' in loader
    assert 'latestRequestControllers.get("orders:list") !== controller' in loader
    for field in (
        'params.customer_id = this.filters.orderCustomer',
        'params.order_number = this.filters.orderNumber',
        'params.customer_po = this.filters.orderCustomerPo',
        'params.product_code = this.filters.orderProductCode',
        'params.product_name = this.filters.orderProductName',
        'params.specification = this.filters.orderSpecification',
    ):
        assert field in loader


def test_mode_change_and_last_page_clamp_execute_real_vue_methods() -> None:
    sizing_start = INDEX.index('orderListPageSize(mode=this.uiMode) {')
    sizing_end = INDEX.index('hasPermission(code) {', sizing_start)
    sizing_methods = INDEX[sizing_start:sizing_end]
    loader_start = INDEX.index('async loadOrders() {')
    loader_end = INDEX.index('async autoReleaseReadyRequisitionHolds', loader_start)
    loader_method = INDEX[loader_start:loader_end]
    script = f"""
const ORDER_LIST_PAGE_SIZE_STANDARD = 12;
const ORDER_LIST_PAGE_SIZE_LARGE = 7;
const latestRequestControllers = new Map();
const sizing = {{ {sizing_methods} }};
let invalidated = [];
let explicitLoads = 0;
const modeContext = {{
  pages:{{orders:4}}, activePage:'orders', orderWorkspace:'queue',
  invalidatePageCache:(...keys)=>invalidated.push(...keys),
  runExplicitPageListLoad:async(page)=>{{ explicitLoads += 1; return page === 'orders'; }},
}};
if (sizing.orderListPageSize.call({{uiMode:'standard'}}, 'standard') !== 12) throw new Error('standard page size');
if (sizing.orderListPageSize.call({{uiMode:'large'}}, 'large') !== 7) throw new Error('large page size');
await sizing.reloadOrdersForUiModeChange.call(modeContext, 'standard', 'large');
if (modeContext.pages.orders !== 1 || explicitLoads !== 1 || JSON.stringify(invalidated) !== '["orders"]') throw new Error('mode reload');

let requestedPages = [];
globalThis.axios = {{get:async(_url, options)=>{{
  requestedPages.push(options.params.page);
  return options.params.page === 3
    ? {{data:{{items:[],total:13,page:3,page_size:12,unfinished_total:2}}}}
    : {{data:{{items:[{{id:12}}],total:13,page:2,page_size:12,unfinished_total:2}}}};
}}}};
const loader = {{ {loader_method} }};
const loadContext = {{
  pages:{{orders:3}}, pageSize:25, activePage:'orders', orderWorkspace:'queue',
  filters:{{orderCustomer:'',orderNumber:'',orderCustomerPo:'',orderProductCode:'',orderProductName:'',orderSpecification:'',orderDateFrom:'',orderDateTo:'',orderDeliveryDateFrom:'',orderDeliveryDateTo:'',orderScope:'active',orderStage:'',orderStatus:'',orderKeyword:'',orderSortBy:'',orderSortDirection:'desc'}},
  orderListPageSize:()=>12,
  beginLatestRequest(key){{ latestRequestControllers.get(key)?.abort(); const controller=new AbortController(); latestRequestControllers.set(key,controller); return controller; }},
  finishLatestRequest(key,controller){{ if(latestRequestControllers.get(key)===controller) latestRequestControllers.delete(key); }},
  isCancelledRequest:()=>false,
  cancelOrderGroupDetailRequests(){{}},
  resetOrderBomDemandSaveState(){{}},
  orderBomDemandSaveState:{{outcomeUncertain:false}},
  orderListGeneration:0,
}};
loadContext.loadOrders = loader.loadOrders.bind(loadContext);
const ok = await loadContext.loadOrders();
if (!ok || loadContext.pages.orders !== 2 || loadContext.orders[0].id !== 12) throw new Error('last page clamp');
if (JSON.stringify(requestedPages) !== '[3,2]') throw new Error('server pages ' + JSON.stringify(requestedPages));
process.stdout.write(JSON.stringify({{sizes:[12,7],requestedPages,page:loadContext.pages.orders}}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == {
        "sizes": [12, 7],
        "requestedPages": [3, 2],
        "page": 2,
    }

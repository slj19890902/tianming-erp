from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_source(start: str, end: str) -> str:
    assert start in INDEX, start
    assert end in INDEX, end
    return INDEX[INDEX.index(start) : INDEX.index(end)]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    target = tmp_path / "p1_61c_frontend_contract.mjs"
    target.write_text(source, encoding="utf-8")
    subprocess.run([node, str(target)], check=True, cwd=ROOT)


def test_orders_default_to_business_queue_and_heat_is_physically_separate() -> None:
    assert 'orderWorkspace: "queue"' in INDEX
    assert "业务队列" in INDEX
    assert "客户热力" in INDEX
    assert 'orderWorkspace === "customer-heat"' in INDEX
    assert 'orderWorkspace === "queue"' in INDEX
    assert 'axios.get("/api/orders/customer-heat"' in INDEX
    assert "customer-heat-card" in INDEX
    assert "customer-heat-band" in INDEX
    assert "customer-heat-legend" in INDEX
    assert 'v-for="level in [5,4,3,2,1]"' in INDEX
    assert "热{{ level }}" in INDEX
    assert "新客户" in INDEX
    assert "不改变订单处理优先级" in INDEX
    heat_start = INDEX.index('<template v-if="orderWorkspace === \'customer-heat\'">')
    heat_end = INDEX.index("<template v-else>", heat_start)
    heat_block = INDEX[heat_start:heat_end]
    assert "status-tag" in heat_block
    assert "backgroundColor" not in heat_block
    assert "customerRowStyle" not in heat_block


def test_heat_view_gates_amount_sort_and_renders_loading_error_empty_large_mode() -> None:
    assert 'v-if="canViewSalesAmounts && customerHeatAmountVisible"' in INDEX
    assert 'v-if="canViewSalesAmounts" value="annual_amount"' in INDEX
    assert "customerHeatLoading" in INDEX
    assert "customerHeatError" in INDEX
    assert "当前筛选没有客户" in INDEX
    assert "重新加载客户热力" in INDEX
    assert ".ui-large .customer-heat-card" in INDEX
    assert ".ui-large .customer-heat-metric" in INDEX
    assert "overflow-wrap: anywhere" in INDEX
    assert "prototype_pending_p1_61c_acceptance" in INDEX


def test_heat_list_is_latest_wins_last_good_and_uses_server_filters(tmp_path: Path) -> None:
    params_method = _method_source(
        "customerHeatRequestParams(page=this.pages.customerHeat)",
        "customerHeatRequestIsCurrent(controller, authGeneration, userId)",
    )
    current_method = _method_source(
        "customerHeatRequestIsCurrent(controller, authGeneration, userId)",
        "async loadCustomerHeat(",
    )
    load_method = _method_source(
        "async loadCustomerHeat(",
        "async openCustomerHeatView()",
    )
    script = f"""
const latestRequestControllers = new Map();
const pending=[];
globalThis.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const methods={{
  {params_method}
  {current_method}
  {load_method}
}};
const vm={{
  activePage:"orders",orderWorkspace:"customer-heat",authGeneration:3,user:{{id:9}},
  filters:{{orderKeyword:"甲",orderCustomer:"",orderStage:"pending_delivery",orderDateFrom:"2026-08-01",orderDateTo:"",orderDeliveryDateFrom:"",orderDeliveryDateTo:"2026-08-31"}},
  customerHeatSortBy:"heat",customerHeatSortDirection:"desc",pages:{{customerHeat:1}},pageSize:25,
  customerHeatItems:[{{customer_id:1,customer_name:"旧结果"}}],customerHeatTotal:1,customerHeatLoading:false,customerHeatError:"",customerHeatAmountVisible:false,
  customerHeatAsOf:"",customerHeatThresholdContract:null,customerHeatThresholdVersion:"",customerHeatThresholdStatus:"",customerHeatAppliedSignature:"",
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
  finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="AbortError";}},pageCacheFresh(){{return false;}},markPageCache(){{}},invalidatePageCache(){{}},
  resetCustomerHeatExpanded(){{}},resetPagePerformanceState(){{this.resetCalled=true;}},resetModalA11ySession(){{}},showToast(){{}},
  errorMessage(error){{return error?.message||"错误";}},...methods,
}};
const first=vm.loadCustomerHeat();
vm.filters.orderKeyword="乙";
const second=vm.loadCustomerHeat();
if(pending.length!==2)throw new Error("heat list request count");
const p=pending[1].options.params;
if(p.keyword!=="乙"||p.stage!=="pending_delivery"||p.order_date_from!=="2026-08-01"||p.delivery_date_to!=="2026-08-31")throw new Error("server filters missing "+JSON.stringify(p));
pending[1].resolve({{data:{{items:[{{customer_id:2,customer_name:"新结果"}}],total:1,page:1,amount_visible:true,as_of:"2026-08-14",threshold_contract:{{status:"prototype_pending_p1_61c_acceptance"}},threshold_version:"v1",threshold_status:"prototype_pending_p1_61c_acceptance"}}}});
await second;
pending[0].resolve({{data:{{items:[{{customer_id:1,customer_name:"过期结果"}}],total:1,page:1}}}});
await first;
if(vm.customerHeatItems[0].customer_id!==2)throw new Error("stale heat response won");
const third=vm.loadCustomerHeat();
pending[2].reject(new Error("临时失败"));
await third;
if(vm.customerHeatItems[0].customer_id!==2||!vm.customerHeatError.includes("临时失败"))throw new Error("last-good not preserved");
"""
    _run_node(script, tmp_path)


def test_heat_orders_expand_on_demand_and_preserve_shared_filters(tmp_path: Path) -> None:
    params_method = _method_source(
        "customerHeatOrderRequestParams(customer, page=1)",
        "customerHeatOrderRequestIsCurrent(customerId, controller, authGeneration, userId)",
    )
    current_method = _method_source(
        "customerHeatOrderRequestIsCurrent(customerId, controller, authGeneration, userId)",
        "async loadCustomerHeatOrders(",
    )
    load_method = _method_source(
        "async loadCustomerHeatOrders(",
        "async toggleCustomerHeatCustomer(customer)",
    )
    script = f"""
const latestRequestControllers=new Map();
const requests=[];
globalThis.axios={{get:(url,options)=>{{requests.push({{url,options}});return Promise.resolve({{data:{{items:[{{id:91,business_status:"pending_delivery"}}],total:1,page:1,page_size:20}}}});}}}};
const methods={{
  {params_method}
  {current_method}
  {load_method}
}};
const vm={{
 activePage:"orders",orderWorkspace:"customer-heat",authGeneration:4,user:{{id:7}},pageSize:25,
 filters:{{orderKeyword:"客户",orderStage:"pending_delivery",orderDateFrom:"2026-08-01",orderDateTo:"2026-08-14",orderDeliveryDateFrom:"",orderDeliveryDateTo:"2026-08-20"}},
 customerHeatOrders:{{}},customerHeatExpanded:{{5:true}},
 beginLatestRequest(key){{const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
 finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
 isCancelledRequest(){{return false;}},errorMessage(e){{return e.message;}},resetPagePerformanceState(){{}},resetModalA11ySession(){{}},showToast(){{}},...methods,
}};
await vm.loadCustomerHeatOrders({{customer_id:5}},{{page:2}});
if(requests.length!==1||requests[0].url!=="/api/orders/customer-heat/5/orders")throw new Error("not lazy endpoint");
const p=requests[0].options.params;
if(p.page!==2||p.stage!=="pending_delivery"||p.order_date_from!=="2026-08-01"||p.delivery_date_to!=="2026-08-20")throw new Error("expanded filters missing "+JSON.stringify(p));
if(vm.customerHeatOrders[5].items[0].id!==91)throw new Error("expanded response missing");
"""
    _run_node(script, tmp_path)


def test_inline_script_remains_valid_javascript(tmp_path: Path) -> None:
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.S)
    assert scripts
    node = shutil.which("node")
    assert node is not None
    target = tmp_path / "p1_61c_index_inline.js"
    target.write_text(scripts[-1], encoding="utf-8")
    subprocess.run([node, "--check", str(target)], check=True, cwd=ROOT)

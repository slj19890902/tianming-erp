from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    match = re.search(
        rf"(?m)^\s{{10}}(?:async\s+)?{re.escape(name)}\([^\n]*\)\s*\{{",
        INDEX,
    )
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"(?m)^\s{10}(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$]*\([^\n]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    source = INDEX[match.end() : match.end() + next_method.start()]
    return source.rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-72B frontend regressions"
    target = tmp_path / name
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


def test_stock_order_entry_moves_next_to_pdf_and_leaves_no_second_create_entry() -> None:
    orders = INDEX.split(
        '<template v-else-if="activePage === \'orders\'">', 1
    )[1].split('<template v-else-if="activePage === \'orders_legacy\'">', 1)[0]
    requisition = INDEX.split(
        '<template v-else-if="activePage === \'requisition\'">', 1
    )[1].split('<template v-else-if="activePage === \'incoming\'">', 1)[0]

    pdf_at = orders.index('@click="openOrderPdfImport">识别PDF订单</button>')
    stock_at = orders.index('@click="openStockReplenishment">库存单</button>')
    assert pdf_at < stock_at
    assert '@click="openStockReplenishment"' not in requisition
    assert "库存单只生成补库报料草稿" in INDEX
    assert "不生成客户订单号" in INDEX


def test_stock_warning_rows_are_grouped_by_customer_in_the_stock_order() -> None:
    assert "stockPolicyCustomerGroups" in INDEX
    assert 'v-for="group in stockPolicyCustomerGroups"' in INDEX
    assert "{{ group.customer_name }}" in INDEX
    assert "库存不足 {{ group.items.length }} 款" in INDEX
    assert 'v-for="policy in group.items"' in INDEX


def test_stock_order_warnings_load_only_after_click_and_are_latest_wins(
    tmp_path: Path,
) -> None:
    open_method = _method_body("openStockReplenishment")
    current_method = _method_body("stockReplenishmentBootstrapRequestIsCurrent")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
global.latestRequestControllers=new Map();
global.createIdempotencyKey=()=>"stock-attempt";
const pending=[];const toasts=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  canRequisition:true,activePage:"orders",authGeneration:7,user:{{id:41}},
  customerOptions:[{{id:1,name:"客户甲"}}],allMaterials:[{{id:1}}],
  stockPolicyWarnings:[{{id:"last-good",customer_id:1,customer_name:"客户甲"}}],
  stockReplenishmentProducts:[],stockReplenishmentCompanions:[],stockReplenishmentBootstrapLoading:false,
  stockReplenishmentBootstrapError:"",modal:null,resetCalls:0,discardCalls:0,
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
  finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="AbortError"||error?.code==="ERR_CANCELED";}},
  errorMessage(error){{return error.message;}},showToast(message,isError){{toasts.push({{message,isError}});}},
  async loadCustomerOptions(){{throw new Error("customer options should already be cached");}},
  async loadMaterials(){{throw new Error("materials should already be cached");}},
  resetPagePerformanceState(){{this.resetCalls+=1;this.stockPolicyWarnings=[];this.modal=null;}},
  resetModalA11ySession(options){{if(options?.discardModal){{this.discardCalls+=1;this.modal=null;}}}},
  syncDesktopWorkspaceUrl(){{}},
}};
vm.stockReplenishmentBootstrapRequestIsCurrent=new FunctionCtor("controller","authGeneration","userId","{{sessionOnly=false}}={{}}",{json.dumps(current_method, ensure_ascii=False)}).bind(vm);
vm.openStockReplenishment=new AsyncFunction("options={{}}",{json.dumps(open_method, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(pending.length===0,"stock warnings loaded before click");
  const old=vm.openStockReplenishment();
  const newest=vm.openStockReplenishment();
  expect(pending.length===2,"two explicit opens did not start two generations");
  expect(pending.every(row=>row.url==="/api/requisition/stock-policies"),"existing warning endpoint was not reused");
  expect(pending.every(row=>row.options?.params?.warning_only===true&&row.options?.params?.target_inventory_type==="finished"),"stock order did not limit warnings to common-box finished products");
  pending[0].resolve({{data:{{items:[{{id:"stale"}}]}}}});
  expect(await old===false,"stale warning response reported success");
  expect(vm.stockPolicyWarnings[0].id==="last-good","stale warning response replaced last-good rows");
  pending[1].resolve({{data:{{items:[{{id:"fresh",customer_id:1,customer_name:"客户甲"}}]}}}});
  expect(await newest===true,"latest warning response did not succeed");
  expect(vm.stockPolicyWarnings[0].id==="fresh","latest warning response was not applied");
  expect(vm.stockReplenishmentBootstrapLoading===false,"latest loading state did not finish");

  const forbidden=vm.openStockReplenishment();
  pending[2].reject(Object.assign(new Error("forbidden"),{{response:{{status:403}}}}));
  expect(await forbidden===false,"forbidden warning request reported success");
  expect(vm.resetCalls===1&&vm.discardCalls===1,"403 did not clear cross-account stock-order state");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-72b-stock-order-latest.js")


def test_stock_order_reuses_replenishment_write_and_never_posts_a_sales_order() -> None:
    save = _method_body("saveStockReplenishmentDraft")
    assert 'axios.post("/api/requisition/stock-replenishment/orders"' in save
    assert 'axios.post("/api/orders"' not in save
    assert "stock_now:false" in save
    assert 'item.location_id = null' in save


def test_session_reset_clears_stock_order_warning_cache_draft_and_attempt() -> None:
    reset = _method_body("resetPagePerformanceState")
    for marker in (
        "this.stockPolicyWarnings = [];",
        "this.stockReplenishmentProducts = [];",
        "this.stockReplenishmentCompanions = [];",
        'this.stockReplenishmentBootstrapError = "";',
        "this.stockReplenishmentBootstrapLoading = false;",
        'this.stockReplenishmentForm = {source_type:"customer_request"',
    ):
        assert marker in reset

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX
    assert next_signature in INDEX
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-36K frontend regressions"
    target = tmp_path / "p1-36k-requisition-pending.js"
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


def test_pending_page_uses_full_list_totals_and_client_side_supplier_selection() -> None:
    page = INDEX.split(
        '<template v-else-if="activePage === \'requisition\'">', 1
    )[1].split('<template v-else-if="activePage === \'incoming\'">', 1)[0]
    assert "待报料 {{ requisitionPendingOverallTotal }}" in page
    assert "全部供应商（{{ requisitionPendingOverallTotal }}）" in page
    assert "全选本页" in page
    assert ':page="pages.requisitionPending"' not in page
    assert ':total="requisitionPendingTotal"' not in page
    assert ':filter-count="requisitionSupplierFilter ? 1 : 0"' not in page
    assert "requisitionPendingLoading" in page
    assert "requisitionPendingError" in page
    assert "selectRequisitionSupplierFilter" in page
    assert "changeRequisitionPendingPage" not in page
    assert (
        "this.ordersUnfinishedTotal + this.requisitionPendingOverallTotal + "
        "this.incomingPendingTotal"
    ) in INDEX
    assert "filteredRequisitionPending()" in INDEX
    filtered = _method_body(
        "filteredRequisitionPending() {", "filteredMergeSuggestions() {"
    )
    assert "if (!supplier) return this.requisitionPending;" in filtered
    assert ".filter(row =>" in filtered


def test_full_list_latest_wins_failure_keeps_last_good_and_success_clears_selection(
    tmp_path: Path,
) -> None:
    current = _method_body(
        "pendingRequisitionRequestIsCurrent(controller, authGeneration, userId) {",
        "requisitionPendingRequestParams(",
    )
    params = _method_body(
        "requisitionPendingRequestParams(page, supplierName) {",
        "clearPendingRequisitionSelection() {",
    )
    clear = _method_body(
        "clearPendingRequisitionSelection() {",
        "applyRequisitionPendingResponse(",
    )
    apply = _method_body(
        "applyRequisitionPendingResponse(data, {supplierName=\"\", clearSelection=false}={}) {",
        "async loadRequisitionPendingPage(",
    )
    load = _method_body(
        "async loadRequisitionPendingPage({page=null, supplierName=null, clearSelection=false}={}) {",
        "async changeRequisitionPendingPage(",
    )
    retry = _method_body(
        "async retryRequisitionPendingPage() {",
        "async loadRequisition(",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
global.latestRequestControllers=new Map();
const pending=[];const toasts=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  authGeneration:4,user:{{id:7}},activePage:"requisition",requisitionTab:"pending",pageSize:25,
  pages:{{requisitionPending:1}},requisitionPending:[{{item_id:1}}],requisitionPendingTotal:51,
  requisitionPendingOverallTotal:51,requisitionPendingLoading:false,requisitionPendingError:"",requisitionPendingRetryScope:"page",
  requisitionPendingAppliedPage:1,requisitionPendingAppliedSupplierFilter:"",
  requisitionSupplierFilter:"",requisitionSupplierCounts:[],selectedPendingKeys:["order_item:1"],
  selectedBomSnapshotIds:["component:1:whole"],requisitionSelected:{{old:true}},
  invalidations:[],marks:[],
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
  finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="AbortError"||error?.code==="ERR_CANCELED";}},
  invalidatePageCache(page){{this.invalidations.push(page);}},markPageCache(page){{this.marks.push(page);}},
  showToast(message,isError){{toasts.push({{message:String(message),isError}});}},errorMessage(error){{return error?.message||String(error);}},
  isPendingRowSelectable(row){{return !row.disabled;}},pendingRowKey(row){{return `order_item:${{row.item_id}}`;}},
  compositeRequisitionComponents(row){{return row.component_requirements||[];}},bomSourceSelectionKey(row){{return row.source_key;}},
}};
vm.pendingRequisitionRequestIsCurrent=new FunctionCtor("controller","authGeneration","userId",{json.dumps(current, ensure_ascii=False)}).bind(vm);
vm.requisitionPendingRequestParams=new FunctionCtor("page","supplierName",{json.dumps(params, ensure_ascii=False)}).bind(vm);
vm.clearPendingRequisitionSelection=new FunctionCtor({json.dumps(clear, ensure_ascii=False)}).bind(vm);
vm.applyRequisitionPendingResponse=new FunctionCtor("data",{json.dumps('{supplierName="", clearSelection=false}={}', ensure_ascii=False)},{json.dumps(apply, ensure_ascii=False)}).bind(vm);
vm.loadRequisitionPendingPage=new AsyncFunction({json.dumps('{page=null, supplierName=null, clearSelection=false}={}', ensure_ascii=False)},{json.dumps(load, ensure_ascii=False)}).bind(vm);
vm.retryRequisitionPendingPage=new AsyncFunction({json.dumps(retry, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const old=vm.loadRequisitionPendingPage({{page:2,clearSelection:true}});
  const latest=vm.loadRequisitionPendingPage({{page:3,clearSelection:true}});
  expect(pending.length===2,"page loads did not issue one GET each");
  expect(pending.every(call=>call.url==="/api/requisition/pending"),"page load called a non-pending endpoint");
  expect(pending.every(call=>Object.keys(call.options.params||{{}}).length===0),"full-list load sent paging or supplier params");
  pending[1].resolve({{data:{{items:[{{item_id:3}}],total:1,supplier_counts:[{{supplier_name:"鸣朋",count:1}}]}}}});
  expect(await latest===true,"latest page failed");
  pending[0].resolve({{data:{{items:[{{item_id:2}}],total:1,supplier_counts:[]}}}});
  expect(await old===false,"stale page was accepted");
  expect(vm.requisitionPending[0].item_id===3&&vm.pages.requisitionPending===1,"stale load replaced latest rows");
  expect(vm.requisitionPendingOverallTotal===1&&vm.requisitionPendingTotal===1,"full-list totals were not applied");
  expect(vm.selectedPendingKeys.length===0&&vm.selectedBomSnapshotIds.length===0,"successful page change kept cross-page selection");
  expect(vm.requisitionPendingLoading===false,"stale request closed latest loading incorrectly");

  vm.selectedPendingKeys=["order_item:3"];
  const failed=vm.loadRequisitionPendingPage({{page:4,clearSelection:true}});
  pending[2].reject(new Error("network down"));
  expect(await failed===false,"failed page reported success");
  expect(vm.requisitionPending[0].item_id===3&&vm.pages.requisitionPending===1,"failure discarded last good list");
  expect(vm.selectedPendingKeys[0]==="order_item:3","failure discarded last good selection");
  expect(vm.requisitionPendingError.includes("network down"),"failure did not expose a Chinese retry state");
  expect(vm.marks.join(",")==="requisition","failed/stale request marked page cache");

  const retrying=vm.retryRequisitionPendingPage();
  expect(Object.keys(pending[3].options.params||{{}}).length===0,"retry sent obsolete paging or supplier params");
  pending[3].resolve({{data:{{items:[{{item_id:4}}],total:1,supplier_counts:[]}}}});
  expect(await retrying===true,"retry did not recover the failed full-list load");
  expect(vm.pages.requisitionPending===1&&vm.requisitionPending[0].item_id===4,"retry response was not applied");
  expect(vm.selectedPendingKeys.length===0,"successful retry kept the previous page selection");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_cold_refresh_auto_release_is_singleflight_and_page_navigation_is_read_only(
    tmp_path: Path,
) -> None:
    auto_release = _method_body(
        "async autoReleaseReadyRequisitionHolds(signal=null) {",
        "pendingRequisitionRequestIsCurrent(controller, authGeneration, userId) {",
    )
    load = _method_body(
        "async loadRequisition({skipAutoRelease=false}={}) {",
        "applyRequisitionHoldSummary(source) {",
    )
    change = _method_body(
        "async changeRequisitionPendingPage(page) {",
        "async selectRequisitionSupplierFilter(",
    )
    assert 'axios.post("/api/requisition/holds/auto-release"' in auto_release
    assert 'axios.get("/api/requisition/holds"' in load
    assert 'axios.get("/api/requisition/pending"' in load
    assert "loadRequisitionPendingPage" in change
    assert "autoReleaseReadyRequisitionHolds" not in change
    assert "/api/requisition/holds" not in change

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();global.requisitionAutoReleaseFlight=null;
let releaseAuto;const calls=[];const toasts=[];
global.axios={{
 post:(url,payload,options)=>{{calls.push({{kind:"post",url}});return new Promise(resolve=>{{releaseAuto=()=>resolve({{data:{{released_hold_ids:[9]}}}});}});}},
 get:async(url,options)=>{{calls.push({{kind:"get",url,options}});if(url.includes("/holds"))return{{data:{{items:[],total:0}}}};return{{data:{{items:[{{item_id:8}}],total:1,overall_total:1,page:1,page_size:25,supplier_counts:[]}}}};}},
}};
const vm={{authGeneration:2,user:{{id:5}},canRequisition:true,activePage:"requisition",requisitionTab:"pending",pageSize:25,pages:{{requisitionPending:1}},
 requisitionSupplierFilter:"",requisitionPendingLoading:false,requisitionPendingError:"",requisitionPendingAppliedPage:1,requisitionPendingAppliedSupplierFilter:"",
 beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
 finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
 isCancelledRequest(error){{return error?.name==="AbortError";}},invalidatePageCache(){{}},markPageCache(){{}},
 pendingRequisitionRequestIsCurrent(c,a,u){{return latestRequestControllers.get("requisition:pending")===c&&!c.signal.aborted&&a===this.authGeneration&&u===this.user.id;}},
 requisitionPendingRequestParams(page,supplier){{return{{page,page_size:this.pageSize}};}},
 applyRequisitionPendingResponse(data){{this.requisitionPending=data.items;this.pages.requisitionPending=data.page;}},
 applyRequisitionHoldsResponse(data){{if((data.auto_released_hold_ids||[]).length)toasts.push(data.auto_released_hold_ids.join(","));}},
 errorMessage(error){{return error?.message||String(error);}},showToast(message){{toasts.push(String(message));}},
}};
vm.autoReleaseReadyRequisitionHolds=new AsyncFunction("signal=null",{json.dumps(auto_release, ensure_ascii=False)}).bind(vm);
vm.loadRequisition=new AsyncFunction("{{skipAutoRelease=false}}={{}}",{json.dumps(load, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
 const first=vm.loadRequisition();const second=vm.loadRequisition();
 await Promise.resolve();
 expect(calls.filter(call=>call.kind==="post").length===1,"rapid refresh posted auto-release twice");
 releaseAuto();
 expect(await first===false,"aborted refresh was accepted");
 expect(await second===true,"latest refresh failed");
 expect(calls.filter(call=>call.kind==="post").length===1,"auto-release was not singleflight");
 expect(calls.filter(call=>call.kind==="get"&&call.url==="/api/requisition/pending").length===1,"latest refresh did not issue exactly one page GET");
 expect(toasts.filter(value=>value==="9").length===1,"released hold toast was not consumed once");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_workbench_failure_retry_restores_holds_and_pending_without_second_auto_release(
    tmp_path: Path,
) -> None:
    auto_release = _method_body(
        "async autoReleaseReadyRequisitionHolds(signal=null) {",
        "pendingRequisitionRequestIsCurrent(controller, authGeneration, userId) {",
    )
    retry = _method_body(
        "async retryRequisitionPendingPage() {",
        "async loadRequisition(",
    )
    load = _method_body(
        "async loadRequisition({skipAutoRelease=false}={}) {",
        "applyRequisitionHoldSummary(source) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();global.requisitionAutoReleaseFlight=null;
let failHolds=true;const calls=[];
global.axios={{
 post:async(url)=>{{calls.push({{kind:"post",url}});return{{data:{{released_hold_ids:[]}}}};}},
 get:async(url,options)=>{{
   calls.push({{kind:"get",url,options}});
   if(url==="/api/requisition/holds"&&failHolds)throw new Error("holds unavailable");
   if(url==="/api/requisition/holds")return{{data:{{items:[{{id:11}}],total:1}}}};
   return{{data:{{items:[{{item_id:12}}],total:1,overall_total:1,page:1,page_size:25,supplier_counts:[]}}}};
 }},
}};
const vm={{authGeneration:3,user:{{id:6}},canRequisition:true,activePage:"requisition",requisitionTab:"pending",pageSize:25,pages:{{requisitionPending:1}},
 requisitionSupplierFilter:"",requisitionPendingLoading:false,requisitionPendingError:"",requisitionPendingRetryScope:"page",
 requisitionPendingAppliedPage:1,requisitionPendingAppliedSupplierFilter:"",requisitionPendingRetryPage:1,requisitionPendingRetrySupplierFilter:"",
 beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
 finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
 isCancelledRequest(error){{return error?.name==="AbortError";}},invalidatePageCache(){{}},marks:0,markPageCache(){{this.marks+=1;}},
 pendingRequisitionRequestIsCurrent(c,a,u){{return latestRequestControllers.get("requisition:pending")===c&&!c.signal.aborted&&a===this.authGeneration&&u===this.user.id;}},
 requisitionPendingRequestParams(page){{return{{page,page_size:this.pageSize}};}},
 applyRequisitionPendingResponse(data){{this.requisitionPending=data.items;this.pages.requisitionPending=data.page;}},
 applyRequisitionHoldsResponse(data){{this.requisitionHolds=data.items;this.requisitionHoldsLoaded=true;}},
 errorMessage(error){{return error?.message||String(error);}},showToast(){{}},
}};
vm.autoReleaseReadyRequisitionHolds=new AsyncFunction("signal=null",{json.dumps(auto_release, ensure_ascii=False)}).bind(vm);
vm.loadRequisition=new AsyncFunction("{{skipAutoRelease=false}}={{}}",{json.dumps(load, ensure_ascii=False)}).bind(vm);
vm.retryRequisitionPendingPage=new AsyncFunction({json.dumps(retry, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
 expect(await vm.loadRequisition().catch(()=>false)===false,"failed workbench reported success");
 expect(vm.requisitionPendingRetryScope==="workbench","workbench failure lost its retry scope");
 expect(vm.requisitionPendingError.includes("holds unavailable"),"workbench error was not retained");
 expect(calls.filter(call=>call.kind==="post").length===1,"initial workbench did not auto-release exactly once");
 failHolds=false;
 expect(await vm.retryRequisitionPendingPage()===true,"workbench retry did not recover");
 expect(calls.filter(call=>call.kind==="post").length===1,"workbench retry repeated auto-release");
 expect(vm.requisitionHoldsLoaded&&vm.requisitionHolds[0].id===11,"workbench retry did not restore holds");
 expect(vm.requisitionPending[0].item_id===12,"workbench retry did not restore pending rows");
 expect(vm.marks===1,"failed workbench marked cache or successful retry did not mark once");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_existing_write_paths_keep_the_workbench_reload_contract() -> None:
    assert "await this.loadRequisition();" in _method_body(
        "async updateMergeGroup(row) {", "pendingSupplierSelectionPayload(row) {"
    )
    assert "this.loadRequisition()," in _method_body(
        "async saveSupplierRequisitionDraft() {", "supplierRequisitionSelectionSignature("
    )
    assert "await this.loadRequisition();" in _method_body(
        "async saveRequisitionHold() {", "async releaseRequisitionHold(row) {"
    )
    assert "await this.loadRequisition();" in _method_body(
        "async autoUseLateFinishedInventory(row) {",
        "async autoUseCustomerBoardPreparation(row) {",
    )

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


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-36L frontend regressions"
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


def test_desktop_pending_uses_server_total_pager_and_current_page_selection() -> None:
    page = INDEX.split(
        '<template v-else-if="activePage === \'incoming\'">', 1
    )[1].split('<template v-else-if="activePage === \'production\'">', 1)[0]

    assert "待入库 {{ incomingPendingTotal + externalIncomingPending.length }}" in page
    assert "全选本页" in page
    assert ':page="pages.incomingPending"' in page
    assert ':total="incomingPendingTotal"' in page
    assert "incomingPendingLoading" in page
    assert "incomingPendingError" in page
    assert "retryIncomingPendingPage" in page
    assert (
        "this.ordersUnfinishedTotal + this.requisitionPendingOverallTotal + "
        "this.incomingPendingTotal"
    ) in INDEX
    assert "incomingPending: 1" in INDEX
    assert "incomingPendingTotal: 0" in INDEX


def test_latest_page_wins_failure_keeps_last_good_and_string_identity(
    tmp_path: Path,
) -> None:
    current = _method_body(
        "pendingIncomingRequestIsCurrent(controller, authGeneration, userId) {",
        "incomingPendingRequestParams(",
    )
    params = _method_body(
        "incomingPendingRequestParams(page) {",
        "applyIncomingPendingResponse(",
    )
    apply = _method_body(
        "applyIncomingPendingResponse(data, {requestedPage=1, clearSelection=false}={}) {",
        "async loadIncomingPendingPage(",
    )
    load = _method_body(
        "async loadIncomingPendingPage({page=null, clearSelection=false, markCache=true}={}) {",
        "async changeIncomingPendingPage(",
    )
    change = _method_body(
        "async changeIncomingPendingPage(page) {",
        "async retryIncomingPendingPage(",
    )
    retry = _method_body(
        "async retryIncomingPendingPage() {",
        "async loadIncoming() {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
global.latestRequestControllers=new Map();
const pending=[];const toasts=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  authGeneration:2,user:{{id:7}},activePage:"incoming",pageSize:25,
  pages:{{incomingPending:1}},incomingPending:[{{item_id:"r1",incoming_quantity:77}}],incomingPendingTotal:51,
  incomingPendingLoading:false,incomingPendingError:"",incomingPendingAppliedPage:1,incomingPendingRetryPage:1,
  incomingSelected:{{r1:true,sr9:true}},invalidations:[],marks:[],
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
  finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="AbortError"||error?.code==="ERR_CANCELED";}},
  invalidatePageCache(page){{this.invalidations.push(page);}},markPageCache(page){{this.marks.push(page);}},
  showToast(message,isError){{toasts.push({{message:String(message),isError}});}},errorMessage(error){{return error?.message||String(error);}},
}};
vm.pendingIncomingRequestIsCurrent=new FunctionCtor("controller","authGeneration","userId",{json.dumps(current, ensure_ascii=False)}).bind(vm);
vm.incomingPendingRequestParams=new FunctionCtor("page",{json.dumps(params, ensure_ascii=False)}).bind(vm);
vm.applyIncomingPendingResponse=new FunctionCtor("data",{json.dumps('{requestedPage=1, clearSelection=false}={}', ensure_ascii=False)},{json.dumps(apply, ensure_ascii=False)}).bind(vm);
vm.loadIncomingPendingPage=new AsyncFunction({json.dumps('{page=null, clearSelection=false, markCache=true}={}', ensure_ascii=False)},{json.dumps(load, ensure_ascii=False)}).bind(vm);
vm.changeIncomingPendingPage=new AsyncFunction("page",{json.dumps(change, ensure_ascii=False)}).bind(vm);
vm.retryIncomingPendingPage=new AsyncFunction({json.dumps(retry, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const old=vm.changeIncomingPendingPage(2);
  const latest=vm.changeIncomingPendingPage(3);
  expect(pending.length===2,"page requests were not started");
  pending[0].resolve({{data:{{items:[{{item_id:"r2",remaining_quantity:2}}],total:52,page:2,page_size:25}}}});
  expect(await old===false,"stale page reported success");
  expect(vm.incomingPending[0].item_id==="r1","stale page replaced last-good data");
  expect(vm.incomingPendingLoading===true,"stale request closed latest loading");
  pending[1].resolve({{data:{{items:[{{item_id:"sr3",remaining_quantity:3,source_type:"stock_replenishment"}}],total:53,page:3,page_size:25}}}});
  expect(await latest===true,"latest page did not succeed");
  expect(vm.pages.incomingPending===3&&vm.incomingPendingTotal===53,"server page/total were not applied");
  expect(vm.incomingPending[0].item_id==="sr3","latest rows were not applied");
  expect(Object.keys(vm.incomingSelected).length===0,"successful page change retained hidden selection");
  expect(vm.locationLoads===undefined,"stock replenishment page must not load exact locations");
  expect(vm.incomingPendingLoading===false,"latest request did not close loading");

  vm.incomingSelected={{sr3:true}};
  vm.incomingPending[0].incoming_quantity=19;
  const failed=vm.changeIncomingPendingPage(4);
  pending[2].reject(new Error("network down"));
  expect(await failed===false,"failed page reported success");
  expect(vm.pages.incomingPending===3&&vm.incomingPendingTotal===53,"failure discarded last-good page metadata");
  expect(vm.incomingPending[0].incoming_quantity===19,"failure discarded the operator draft");
  expect(vm.incomingSelected.sr3===true,"failure discarded current-page selection");
  expect(vm.incomingPendingError.includes("network down"),"failure did not expose retry error");

  const recovered=vm.retryIncomingPendingPage();
  pending[3].resolve({{data:{{items:[{{item_id:"r4",remaining_quantity:4}}],total:26,page:2,page_size:25}}}});
  expect(await recovered===true,"retry did not recover");
  expect(vm.pages.incomingPending===2&&vm.incomingPending[0].item_id==="r4","server-clamped page was not authoritative");
  expect(Object.keys(vm.incomingSelected).length===0,"clamped page retained hidden selection");

  vm.incomingSelected={{r4:true,sr99:true}};
  vm.applyIncomingPendingResponse({{items:[{{item_id:"r4",remaining_quantity:4}}],total:1,page:2}},{{requestedPage:2,clearSelection:false}});
  expect(vm.incomingSelected.r4===true&&!vm.incomingSelected.sr99,"string route identity cleanup is incorrect");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-36l-desktop-page-race.js")


def test_full_refresh_is_concurrent_but_page_and_write_reload_only_paper(
    tmp_path: Path,
) -> None:
    load = _method_body("async loadIncoming() {", "externalIncomingDraftKey(")
    change = _method_body(
        "async changeIncomingPendingPage(page) {",
        "async retryIncomingPendingPage(",
    )
    refresh = _method_body(
        "async refreshIncomingAfterWrite() {",
        "incomingProjectedVariance(",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const calls=[];const pending=[];
const deferred=kind=>new Promise(resolve=>{{calls.push(kind);pending.push({{kind,resolve}});}});
const vm={{
  activePage:"incoming",incomingPendingAppliedPage:2,incomingPendingError:"",incomingReceivedLoaded:false,marks:0,
  loadIncomingPendingPage(options){{this.lastPendingOptions=options;return deferred("paper");}},
  loadExternalIncoming(){{return deferred("external");}},
  loadKpi(){{calls.push("kpi");return Promise.resolve(true);}},
  loadIncomingReceived(){{calls.push("received");return Promise.resolve(true);}},
  markPageCache(page){{if(page==="incoming")this.marks+=1;}},
}};
vm.loadIncoming=new AsyncFunction({json.dumps(load, ensure_ascii=False)}).bind(vm);
vm.changeIncomingPendingPage=new AsyncFunction("page",{json.dumps(change, ensure_ascii=False)}).bind(vm);
vm.refreshIncomingAfterWrite=new AsyncFunction({json.dumps(refresh, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const full=vm.loadIncoming();
  expect(calls.join("|")==="paper|external","cold/manual refresh did not start paper and external concurrently");
  pending.find(row=>row.kind==="paper").resolve(true);
  pending.find(row=>row.kind==="external").resolve(true);
  expect(await full===true&&vm.marks===1,"successful full refresh did not complete/cache once");

  calls.length=0;pending.length=0;
  const page=vm.changeIncomingPendingPage(3);
  expect(calls.join("|")==="paper","page navigation reloaded external packaging");
  expect(vm.lastPendingOptions.page===3&&vm.lastPendingOptions.clearSelection===true,"page navigation contract is wrong");
  pending[0].resolve(true);await page;

  calls.length=0;pending.length=0;
  const writeRefresh=vm.refreshIncomingAfterWrite();
  expect(calls.join("|")==="paper|kpi","paper write refresh reloaded external packaging or skipped KPI");
  expect(vm.lastPendingOptions.page===2,"write refresh did not request the applied page");
  pending[0].resolve(true);await writeRefresh;
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-36l-desktop-load-routing.js")


def test_existing_write_and_external_purchase_endpoints_are_unchanged() -> None:
    refresh = _method_body(
        "async refreshIncomingAfterWrite() {",
        "incomingProjectedVariance(",
    )
    revert = _method_body("async revertIncoming(row) {", "async openIncomingProductionCard(")

    assert "loadIncomingPendingPage" in refresh
    assert "loadExternalIncoming" not in refresh
    assert "loadIncomingPendingPage" in revert
    assert "const pendingRefresh=this.loadIncomingPendingPage" in revert
    assert "Promise.all([pendingRefresh, this.loadIncomingHistory()" in revert
    assert "Promise.all([pendingRefresh, this.loadIncomingReceived({force:true})" in revert
    assert 'axios.put("/api/incoming/batch-receive"' in INDEX
    assert "axios.put(`/api/incoming/receive/${row.item_id}`,payload)" in INDEX
    assert "axios.put(`/api/incoming/receipt-items/${row.pending_receipt_item_id}/accept-short`,{})" in INDEX
    assert 'axios.get("/api/external-packaging-purchases/pending-receipts"' in INDEX
    assert "axios.post(`/api/external-packaging-purchases/${purchase.id}/receipts`" in INDEX


def test_desktop_session_reset_clears_account_bound_incoming_last_good(
    tmp_path: Path,
) -> None:
    reset = _method_body(
        "resetPagePerformanceState() {",
        "pageCacheFresh(page) {",
    )
    script = f"""
const FunctionCtor=Function;
global.latestRequestControllers=new Map();
global.requisitionAutoReleaseFlight={{old:true}};
global.searchDebounceTimers=new Map();
global.pageSearchGenerations=new Map();
global.pinyinSearchTextCache=new Map();
const vm={{
  loginAttemptSequence:1,pageLoadSequence:1,loading:true,pageCacheUpdatedAt:{{incoming:1}},
  orderGroupDetails:{{}},orderGroupDetailLoading:{{}},orderGroupDetailErrors:{{}},
  deliveryDetailRequestSequence:0,deliveryDetailState:{{}},expandedDeliveryRows:{{}},
  supplierRequisitionPreviewLoading:true,pages:{{incomingPending:3}},
  incomingPending:[{{item_id:"r-secret"}}],incomingPendingTotal:51,incomingPendingLoading:true,
  incomingPendingError:"old",incomingPendingAppliedPage:3,incomingPendingRetryPage:4,
  incomingSelected:{{"r-secret":true}},incomingReceived:[{{customer_name:"old"}}],
  incomingReceivedLoaded:true,incomingReceivedLoading:true,incomingReceivedError:"old",
  incomingHistory:[{{customer_name:"old"}}],incomingHistoryTotal:1,incomingLocations:[{{id:1}}],
  incomingLocationsLoading:true,incomingLocationsError:"old",incomingReceiptLocations:[{{id:2}}],
  incomingReceiptLocationsLoading:true,incomingReceiptLocationsError:"old",
  externalIncomingPending:[{{id:3}}],externalIncomingDrafts:{{x:1}},externalIncomingLoading:true,
  externalIncomingError:"old",externalIncomingSavingId:3,
  cancelOrderGroupDetailRequests(){{}},
}};
new FunctionCtor({json.dumps(reset, ensure_ascii=False)}).call(vm);
if(vm.incomingPending.length||vm.incomingPendingTotal||Object.keys(vm.incomingSelected).length)throw new Error("pending last-good survived session reset");
if(vm.pages.incomingPending!==1||vm.incomingPendingAppliedPage!==1||vm.incomingPendingRetryPage!==1)throw new Error("pending page survived session reset");
if(vm.incomingReceived.length||vm.incomingHistory.length||vm.externalIncomingPending.length)throw new Error("incoming account data survived session reset");
"""
    _run_node(script, tmp_path, "p1-36l-desktop-session-reset.js")

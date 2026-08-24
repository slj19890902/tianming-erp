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

    assert "待入库 {{ incomingPendingTotal }}" in page
    assert "待入库 {{ incomingPendingTotal + externalIncomingPending.length }}" not in page
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
  incomingSelected:{{r1:true,sr9:true}},incomingReceiveAttempts:{{}},invalidations:[],marks:[],
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


def test_board_refresh_page_and_write_reload_only_paper(
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
  activePage:"incoming",incomingWorkspace:"board",incomingPendingAppliedPage:2,incomingPendingError:"",incomingReceivedLoaded:false,
  loadIncomingPendingPage(options){{this.lastPendingOptions=options;return deferred("paper");}},
  loadExternalIncoming(){{calls.push("external");return Promise.resolve(true);}},
  loadKpi(){{calls.push("kpi");return Promise.resolve(true);}},
  loadIncomingReceived(){{calls.push("received");return Promise.resolve(true);}},
}};
vm.loadIncoming=new AsyncFunction({json.dumps(load, ensure_ascii=False)}).bind(vm);
vm.changeIncomingPendingPage=new AsyncFunction("page",{json.dumps(change, ensure_ascii=False)}).bind(vm);
vm.refreshIncomingAfterWrite=new AsyncFunction({json.dumps(refresh, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const full=vm.loadIncoming();
  expect(calls.join("|")==="paper","board cold/manual refresh requested external packaging");
  expect(vm.lastPendingOptions.page===2&&vm.lastPendingOptions.clearSelection===false,"board refresh did not keep the applied paper page");
  pending[0].resolve(true);
  expect(await full===true,"successful paper refresh did not report success");

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


def test_desktop_single_receive_reuses_key_and_never_reports_refresh_as_write_failure(
    tmp_path: Path,
) -> None:
    receive = _method_body(
        "async receiveIncoming(row) {",
        "async acceptShortIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const requests=[];const toasts=[];const refreshResults=[false,true,true];
global.confirm=()=>true;
global.createIdempotencyKey=()=>`attempt-${{++keySequence}}`;
global.axios={{put:async(url,payload)=>{{
  requests.push({{url,payload:JSON.parse(JSON.stringify(payload))}});
  if(requests.length===1)throw Object.assign(new Error("Request failed with status code 500"),{{response:{{status:500}}}});
  return {{data:{{material_status:"received",remaining_quantity:0}}}};
}}}};
let keySequence=0;
const vm={{
  incomingReceiveAttempts:{{}},
  async ensureAutomaticPurchaseReceiptFact(){{}},
  incomingPayload(row){{return {{item_id:row.item_id,received_quantity:Number(row.incoming_quantity),resolution_action:null,resolution_reason:null,surplus_location_id:null}};}},
  showIncomingNextStepGuide(){{}},
  async refreshIncomingAfterWrite(){{return refreshResults.shift();}},
  showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},
  errorMessage(error){{return error?.message||String(error);}},
}};
vm.receiveIncoming=new AsyncFunction("row",{json.dumps(receive, ensure_ascii=False)}).bind(vm);
const row={{item_id:99,incoming_quantity:20,source_type:"supplier_order"}};
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  await vm.receiveIncoming(row);
  expect(requests.length===1,"first write was not attempted");
  expect(!("item_id" in requests[0].payload),"path item_id leaked into strict single-receive body");
  const firstKey=requests[0].payload.idempotency_key;
  expect(firstKey==="attempt-1","attempt did not own the idempotency key");
  expect(toasts.at(-1).message.includes("结果暂未确认"),"5xx was not described as uncertain");
  await vm.receiveIncoming(row);
  expect(requests.length===2,"same payload was not retried");
  expect(requests[1].payload.idempotency_key===firstKey,"same payload used a new idempotency key");
  expect(toasts.at(-1).message.includes("实收已成功")&&toasts.at(-1).message.includes("刷新失败"),"committed write was misreported");
  expect(!toasts.at(-1).message.includes("Request failed"),"raw request failure leaked after commit");
  await vm.receiveIncoming(row);
  expect(requests.length===2,"committed attempt submitted a duplicate write");
  expect(!vm.incomingReceiveAttempts["99"],"authoritative refresh did not clear the attempt");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-56-desktop-receive-attempt.js")


def test_desktop_single_receive_keeps_uncertain_attempt_until_reconciled_and_single_flights(
    tmp_path: Path,
) -> None:
    receive = _method_body(
        "async receiveIncoming(row) {",
        "async acceptShortIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const requests=[];const toasts=[];let keySequence=0;let resolveThird;
global.confirm=()=>true;
global.createIdempotencyKey=()=>`single-${{++keySequence}}`;
global.axios={{put:(url,payload)=>{{
  requests.push({{url,payload:JSON.parse(JSON.stringify(payload))}});
  if(requests.length===1)return Promise.reject(Object.assign(new Error("server uncertain"),{{response:{{status:500}}}}));
  if(requests.length===2)return Promise.reject(Object.assign(new Error("validation failed"),{{response:{{status:422}}}}));
  if(requests.length===3)return new Promise(resolve=>{{resolveThird=resolve;}});
  throw new Error("unexpected duplicate write");
}}}};
const vm={{
  incomingReceiveAttempts:{{}},
  async ensureAutomaticPurchaseReceiptFact(){{}},
  incomingPayload(row){{return {{item_id:row.item_id,received_quantity:Number(row.incoming_quantity),resolution_action:null,resolution_reason:null,surplus_location_id:null}};}},
  showIncomingNextStepGuide(){{}},
  async refreshIncomingAfterWrite(){{return true;}},
  showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},
  errorMessage(error){{return error?.message||String(error);}},
}};
vm.receiveIncoming=new AsyncFunction("row",{json.dumps(receive, ensure_ascii=False)}).bind(vm);
const row={{item_id:99,incoming_quantity:20,source_type:"supplier_order"}};
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  await vm.receiveIncoming(row);
  expect(requests.length===1,"initial uncertain write was not attempted");
  const uncertainKey=requests[0].payload.idempotency_key;

  row.incoming_quantity=21;
  await vm.receiveIncoming(row);
  expect(requests.length===1,"changed uncertain payload was submitted with a new key");
  expect(toasts.at(-1).message.includes("不能修改数量或处理方式"),"changed uncertain payload was not rejected clearly");

  row.incoming_quantity=20;
  await vm.receiveIncoming(row);
  expect(requests.length===2,"same uncertain payload was not retried for the 4xx case");
  expect(requests[1].payload.idempotency_key===uncertainKey,"same uncertain payload did not retain its key before 4xx");
  expect(!vm.incomingReceiveAttempts["99"],"explicit 4xx did not release the rejected attempt");
  expect(!toasts.at(-1).message.includes("结果暂未确认"),"explicit 4xx was described as an uncertain write");

  // A rejected request never entered the receiving service, so the operator
  // may correct the payload and submit a new attempt with a new key.
  row.incoming_quantity=21;
  const first=vm.receiveIncoming(row);
  const doubleClick=vm.receiveIncoming(row);
  await new Promise(resolve=>setImmediate(resolve));
  expect(requests.length===3,"double click submitted more than one in-flight write");
  expect(vm.incomingReceiveAttempts["99"]?.saving===true,"single-flight attempt was not marked saving");
  expect(requests[2].payload.idempotency_key!==uncertainKey,"new payload after reconciliation reused the old key");
  resolveThird({{data:{{material_status:"received",remaining_quantity:0}}}});
  await Promise.all([first,doubleClick]);
  expect(requests.length===3,"double click produced a late duplicate write");
  expect(!vm.incomingReceiveAttempts["99"],"successful single-flight write did not clear its attempt");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-56-desktop-single-guards.js")


def test_desktop_batch_receive_recovers_frozen_payload_before_current_selection(
    tmp_path: Path,
) -> None:
    batch_receive = _method_body(
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const requests=[];const toasts=[];const refreshResults=[false,true];
let keySequence=0;let rejectFirst;let rejectMissingRowsRetry;
global.confirm=()=>true;
global.createIdempotencyKey=()=>`batch-key-${{++keySequence}}`;
global.axios={{put:(url,payload)=>{{
  requests.push({{url,payload:JSON.parse(JSON.stringify(payload))}});
  if(requests.length===1)return new Promise((_resolve,reject)=>{{rejectFirst=reject;}});
  if(requests.length===2)return new Promise((_resolve,reject)=>{{rejectMissingRowsRetry=reject;}});
  if(requests.length===3)return Promise.resolve({{data:{{
    succeeded:2,failed:0,
    results:[
      {{item_id:"r1",success:true,item:{{material_status:"received"}}}},
      {{item_id:"r2",success:true,item:{{material_status:"received"}}}},
    ],
  }}}});
  throw new Error("unexpected duplicate batch write");
}}}};
const rows=[
  {{item_id:"r1",incoming_quantity:10,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",product_code:"A"}},
  {{item_id:"r2",incoming_quantity:20,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",product_code:"B"}},
];
const vm={{
  incomingPending:rows,
  incomingSelected:{{r1:true,r2:true}},
  incomingBatchReceiveAttempt:null,
  canReceiveIncoming(row){{return row.material_status==="pending";}},
  async ensureAutomaticPurchaseReceiptFact(){{}},
  incomingPayload(row){{return {{item_id:row.item_id,received_quantity:Number(row.incoming_quantity),resolution_action:null,resolution_reason:null,surplus_location_id:null}};}},
  showIncomingNextStepGuide(){{}},
  async refreshIncomingAfterWrite(){{return refreshResults.shift();}},
  showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},
  errorMessage(error){{return error?.message||String(error);}},
}};
vm.batchReceiveIncoming=new AsyncFunction({json.dumps(batch_receive, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const first=vm.batchReceiveIncoming();
  const doubleClick=vm.batchReceiveIncoming();
  await new Promise(resolve=>setImmediate(resolve));
  expect(requests.length===1,"batch double click submitted more than one in-flight write");
  expect(vm.incomingBatchReceiveAttempt?.saving===true,"batch attempt was not marked saving");
  rejectFirst(Object.assign(new Error("batch request failed with 500"),{{response:{{status:500}}}}));
  await Promise.all([first,doubleClick]);
  const firstPayload=requests[0].payload;
  expect(firstPayload.idempotency_key&&firstPayload.items.every(item=>item.idempotency_key),"batch attempt did not persist batch and line keys");
  expect(keySequence===3,"initial batch did not create exactly one batch key and two line keys");

  vm.incomingPending=[];
  vm.incomingSelected={{}};
  const missingRowsRetry=vm.batchReceiveIncoming();
  const missingRowsDoubleClick=vm.batchReceiveIncoming();
  expect(requests.length===2,"missing rows or empty selection prevented the frozen batch replay");
  expect(JSON.stringify(requests[1].payload)===JSON.stringify(firstPayload),"missing-row recovery did not replay the frozen batch and line keys");
  expect(keySequence===3,"missing-row recovery generated new idempotency keys");
  expect(vm.incomingBatchReceiveAttempt?.saving===true,"missing-row recovery was not single-flight");
  rejectMissingRowsRetry(Object.assign(new Error("batch retry still uncertain"),{{response:{{status:500}}}}));
  await Promise.all([missingRowsRetry,missingRowsDoubleClick]);

  const newRow={{item_id:"r-new",incoming_quantity:99,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",product_code:"NEW"}};
  vm.incomingPending=[newRow];
  vm.incomingSelected={{"r-new":true}};
  await vm.batchReceiveIncoming();
  expect(requests.length===3,"new selection prevented recovery of the old uncertain batch");
  expect(JSON.stringify(requests[2].payload)===JSON.stringify(firstPayload),"new selection replaced the frozen old batch payload");
  expect(!requests[2].payload.items.some(item=>item.item_id==="r-new"),"newly selected row leaked into the old batch recovery");
  expect(keySequence===3,"new selection caused recovery to allocate new keys");
  expect(vm.incomingBatchReceiveAttempt?.committed===true,"committed batch was not retained after refresh failure");
  expect(toasts.at(-1).message.includes("批量实收已成功")&&toasts.at(-1).message.includes("刷新失败"),"committed batch refresh failure was misreported");

  vm.incomingSelected={{"r-new":true}};
  await vm.batchReceiveIncoming();
  expect(requests.length===3,"committed batch was written again while recovering the list");
  expect(vm.incomingBatchReceiveAttempt===null,"successful recovery refresh did not clear the committed batch attempt");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-56-desktop-batch-guards.js")


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
global.today=()=>"2026-08-13";
global.plusDays=()=>"2026-08-20";
global.blankReceiptReminderEditor=()=>({{}});
const vm={{
  loginAttemptSequence:1,pageLoadSequence:1,loading:true,pageCacheUpdatedAt:{{incoming:1,"incoming:external-packaging":2}},
  orderGroupDetails:{{}},orderGroupDetailLoading:{{}},orderGroupDetailErrors:{{}},
  deliveryDetailRequestSequence:0,deliveryDetailState:{{}},expandedDeliveryRows:{{}},
  supplierRequisitionPreviewLoading:true,pages:{{incomingPending:3,externalIncoming:4}},
  incomingWorkspace:"external-packaging",
  incomingPending:[{{item_id:"r-secret"}}],incomingPendingTotal:51,incomingPendingLoading:true,
  incomingPendingError:"old",incomingPendingAppliedPage:3,incomingPendingRetryPage:4,
  incomingSelected:{{"r-secret":true}},incomingReceived:[{{customer_name:"old"}}],
  incomingReceiveAttempts:{{"r-secret":{{idempotency_key:"secret",committed:true}}}},
  incomingBatchReceiveAttempt:{{signature:"secret-batch",request_payload:{{idempotency_key:"secret"}},saving:false,committed:true}},
  incomingReceivedLoaded:true,incomingReceivedLoading:true,incomingReceivedError:"old",
  incomingHistory:[{{customer_name:"old"}}],incomingHistoryTotal:1,incomingLocations:[{{id:1}}],
  incomingLocationsLoading:true,incomingLocationsError:"old",incomingReceiptLocations:[{{id:2}}],
  incomingReceiptLocationsLoading:true,incomingReceiptLocationsError:"old",
  externalIncomingPending:[{{id:3}}],externalIncomingDrafts:{{x:1}},externalIncomingLoading:true,
  externalIncomingError:"old",externalIncomingFilter:"secret",externalIncomingLoaded:true,externalIncomingSavingId:3,
  cancelOrderGroupDetailRequests(){{}},
}};
new FunctionCtor({json.dumps(reset, ensure_ascii=False)}).call(vm);
if(vm.incomingPending.length||vm.incomingPendingTotal||Object.keys(vm.incomingSelected).length)throw new Error("pending last-good survived session reset");
if(Object.keys(vm.incomingReceiveAttempts).length)throw new Error("incoming receive attempts survived session reset");
if(vm.incomingBatchReceiveAttempt!==null)throw new Error("incoming batch receive attempt survived session reset");
if(vm.pages.incomingPending!==1||vm.incomingPendingAppliedPage!==1||vm.incomingPendingRetryPage!==1)throw new Error("pending page survived session reset");
if(vm.incomingReceived.length||vm.incomingHistory.length||vm.externalIncomingPending.length)throw new Error("incoming account data survived session reset");
if(vm.incomingWorkspace!=="board"||vm.externalIncomingFilter||vm.externalIncomingLoaded||vm.pages.externalIncoming!==1)throw new Error("external incoming workspace/filter/page survived session reset");
if(Object.keys(vm.pageCacheUpdatedAt).length)throw new Error("incoming cache keys survived session reset");
"""
    _run_node(script, tmp_path, "p1-36l-desktop-session-reset.js")

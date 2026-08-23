from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STANDALONE_INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX
    assert next_signature in INDEX
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-55B frontend regressions"
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


def test_desktop_dom_is_split_and_standalone_incoming_stays_paper_only() -> None:
    page = INDEX.split(
        '<template v-else-if="activePage === \'incoming\'">', 1
    )[1].split('<template v-else-if="activePage === \'production\'">', 1)[0]
    board_title = '<strong class="incoming-page-title">纸板收料</strong>'
    packaging_title = '<page-head title="包材收料"'
    assert "<template v-if=\"incomingWorkspace==='external-packaging'\">" in page
    assert packaging_title in page
    assert board_title in page
    assert '@click="returnToBoardIncoming">返回纸板收料</button>' in page
    assert '@click="openExternalPackagingIncoming">包材收料</button>' in page

    packaging, board = page.split(board_title, 1)
    assert 'class="external-incoming-panel"' in packaging
    assert 'v-for="purchase in pagedExternalIncoming"' in packaging
    assert 'class="external-incoming-panel"' not in board
    assert "externalIncomingPending.length" not in board
    assert "待入库 {{ incomingPendingTotal }}" in board
    assert "toggleAllIncoming" in board
    assert "incoming-compact-table" in board

    assert "/api/external-packaging-purchases/pending-receipts" not in STANDALONE_INCOMING
    assert "incomingWorkspace" not in STANDALONE_INCOMING
    assert "externalIncomingPending" not in STANDALONE_INCOMING
    assert "包材收料" not in STANDALONE_INCOMING


def test_default_board_loader_never_requests_the_full_packaging_list() -> None:
    load_page = _method_body(
        "async loadPage(page, { force=false, supplierPromise=null } = {}) {",
        "refreshCurrent() {",
    )
    load_board = _method_body("async loadIncoming() {", "externalIncomingDraftKey(")
    refresh_tab = _method_body("async refreshIncomingTab() {", "async refreshIncomingAfterWrite()")
    write_refresh = _method_body("async refreshIncomingAfterWrite() {", "incomingProjectedVariance(")

    assert 'if (this.incomingWorkspace === "external-packaging") await requirePageLoad(this.loadExternalIncoming());' in load_page
    assert 'else await requirePageLoad(this.loadIncoming());' in load_page
    assert "loadIncomingPendingPage" in load_board
    assert "loadExternalIncoming" not in load_board
    assert "/api/external-packaging-purchases/pending-receipts" not in load_board
    assert "return this.loadIncoming();" in refresh_tab
    assert "loadExternalIncoming" not in refresh_tab
    assert "loadIncomingPendingPage" in write_refresh
    assert "loadExternalIncoming" not in write_refresh


def test_url_restore_navigation_and_page_cache_keys_are_isolated(
    tmp_path: Path,
) -> None:
    initial = _method_body(
        "initialIncomingWorkspaceFromLocation(page) {",
        "orderStageDeepLinkRequest() {",
    )
    sync_url = _method_body(
        "syncDesktopWorkspaceUrl(page, subpage=\"\") {",
        "redirectAfterLogin() {",
    )
    open_packaging = _method_body(
        "async openExternalPackagingIncoming() {",
        "async returnToBoardIncoming() {",
    )
    return_board = _method_body(
        "async returnToBoardIncoming() {",
        "async loadExternalIncoming() {",
    )
    load_page = _method_body(
        "async loadPage(page, { force=false, supplierPromise=null } = {}) {",
        "refreshCurrent() {",
    )
    go = _method_body("async go(page) {", "async loadInitialPageResources()")

    assert '"incoming:external-packaging"' in load_page
    assert "this.incomingWorkspace === \"external-packaging\"" in load_page
    assert 'if (page === "incoming") {' in go
    assert 'this.incomingWorkspace = "board";' in go
    assert 'this.syncDesktopWorkspaceUrl("incoming");' in go

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
global.window={{
  location:{{href:"https://erp.local/?page=incoming&subpage=external-packaging",search:"?page=incoming&subpage=external-packaging"}},
  history:{{state:null,replaceState(_state,_title,url){{this.lastUrl=String(url);}}}},
}};
const vm={{
  incomingWorkspace:"external-packaging",pageLoadSequence:0,authGeneration:3,user:{{id:9}},loading:false,
  fresh:new Set(),invalidations:[],marks:[],calls:[],cancelled:[],incomingSelected:{{r7:true}},
  externalIncomingDrafts:{{"3:4":7}},
  currentPageSearchGeneration(){{return 0;}},
  pageCacheFresh(key){{return this.fresh.has(key);}},
  invalidatePageCache(key){{this.invalidations.push(key);}},
  markPageCache(key){{this.marks.push(key);this.fresh.add(key);}},
  loadExternalIncoming(){{this.calls.push("external");return Promise.resolve(true);}},
  loadIncoming(){{this.calls.push("paper");return Promise.resolve(true);}},
  pageAllowed(){{return true;}},cancelLatestRequest(key){{this.cancelled.push(key);}},
  showToast(message){{throw new Error(String(message));}},errorMessage(error){{return String(error?.message||error);}},
}};
vm.initialIncomingWorkspaceFromLocation=new FunctionCtor("page",{json.dumps(initial, ensure_ascii=False)}).bind(vm);
vm.syncDesktopWorkspaceUrl=new FunctionCtor("page",{json.dumps('subpage=""')},{json.dumps(sync_url, ensure_ascii=False)}).bind(vm);
vm.openExternalPackagingIncoming=new AsyncFunction({json.dumps(open_packaging, ensure_ascii=False)}).bind(vm);
vm.returnToBoardIncoming=new AsyncFunction({json.dumps(return_board, ensure_ascii=False)}).bind(vm);
vm.loadPage=new AsyncFunction("page","{{ force=false, supplierPromise=null }} = {{}}",{json.dumps(load_page, ensure_ascii=False)}).bind(vm);
(async()=>{{
  expect(vm.initialIncomingWorkspaceFromLocation("incoming")==="external-packaging","refresh did not restore the packaging child page");
  expect(vm.initialIncomingWorkspaceFromLocation("orders")==="board","foreign page restored the incoming child state");

  expect(await vm.loadPage("incoming")===true,"direct child-page load failed");
  expect(vm.calls.join("|")==="external","direct child-page load requested paper data");
  expect(vm.invalidations.join("|")==="incoming:external-packaging","child-page load invalidated the paper cache");
  expect(vm.marks.join("|")==="incoming:external-packaging","child-page load marked the paper cache");

  vm.calls=[];vm.incomingWorkspace="board";
  expect(await vm.openExternalPackagingIncoming()===true,"packaging button failed");
  expect(vm.incomingWorkspace==="external-packaging"&&vm.calls.length===0,"fresh package cache triggered a duplicate full-list request");
  expect(vm.externalIncomingDrafts["3:4"]===7,"fresh package cache discarded the same-account receipt draft");
  expect(window.history.lastUrl.includes("page=incoming")&&window.history.lastUrl.includes("subpage=external-packaging"),"packaging URL was not refreshable");

  vm.calls=[];vm.fresh.delete("incoming:external-packaging");vm.incomingWorkspace="board";
  expect(await vm.openExternalPackagingIncoming()===true,"uncached packaging button failed");
  expect(vm.calls.join("|")==="external","uncached packaging button did not request the package list once");

  vm.calls=[];vm.fresh.add("incoming");
  expect(await vm.returnToBoardIncoming()===true,"cached board return failed");
  expect(vm.calls.length===0,"cached board return reloaded paper or packaging");
  expect(vm.incomingSelected.r7===true,"returning to paper discarded its current-page selection");
  expect(!window.history.lastUrl.includes("subpage="),"board URL retained the packaging subpage");

  vm.incomingWorkspace="external-packaging";vm.fresh=new Set(["incoming:external-packaging"]);vm.calls=[];
  expect(await vm.returnToBoardIncoming()===true,"uncached board return failed");
  expect(vm.calls.join("|")==="paper","packaging cache falsely satisfied the paper cache");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55b-route-cache.js")


def test_external_filter_pager_latest_wins_last_good_and_retry(tmp_path: Path) -> None:
    filtered = _method_body("filteredExternalIncoming() {", "pagedExternalIncoming() {")
    paged = _method_body("pagedExternalIncoming() {", "filteredMergeSuggestions() {")
    draft_key = _method_body(
        "externalIncomingDraftKey(purchaseId, purchaseItemId) {",
        "applyExternalIncomingOverview(",
    )
    apply_overview = _method_body(
        "applyExternalIncomingOverview(data) {",
        "externalIncomingRequestIsCurrent(",
    )
    is_current = _method_body(
        "externalIncomingRequestIsCurrent(controller, authGeneration, userId) {",
        "async openExternalPackagingIncoming() {",
    )
    load_external = _method_body(
        "async loadExternalIncoming() {",
        "async receiveExternalPurchase(purchase) {",
    )

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
global.latestRequestControllers=new Map();
const requests=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>requests.push({{url,options,resolve,reject}}))}};
const vm={{
  authGeneration:4,user:{{id:12}},activePage:"incoming",incomingWorkspace:"external-packaging",
  pageSize:1,pages:{{externalIncoming:2}},externalIncomingFilter:"",externalIncomingLoaded:true,
  externalIncomingPending:[{{id:90,supplier_name:"last-good",purchase_number:"EP-old",items:[]}}],
  externalIncomingDrafts:{{old:1}},externalIncomingLoading:false,externalIncomingError:"",marks:[],invalidations:[],
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="AbortError"||error?.code==="ERR_CANCELED";}},
  invalidatePageCache(key){{this.invalidations.push(key);}},markPageCache(key){{this.marks.push(key);}},
  errorMessage(error){{return String(error?.message||error);}},resetPagePerformanceState(){{this.reset=true;}},
}};
vm.externalIncomingDraftKey=new FunctionCtor("purchaseId","purchaseItemId",{json.dumps(draft_key, ensure_ascii=False)}).bind(vm);
vm.applyExternalIncomingOverview=new FunctionCtor("data",{json.dumps(apply_overview, ensure_ascii=False)}).bind(vm);
vm.externalIncomingRequestIsCurrent=new FunctionCtor("controller","authGeneration","userId",{json.dumps(is_current, ensure_ascii=False)}).bind(vm);
vm.loadExternalIncoming=new AsyncFunction({json.dumps(load_external, ensure_ascii=False)}).bind(vm);
Object.defineProperty(vm,"filteredExternalIncoming",{{get:new FunctionCtor({json.dumps(filtered, ensure_ascii=False)}).bind(vm)}});
Object.defineProperty(vm,"pagedExternalIncoming",{{get:new FunctionCtor({json.dumps(paged, ensure_ascii=False)}).bind(vm)}});
(async()=>{{
  const old=vm.loadExternalIncoming();
  const latest=vm.loadExternalIncoming();
  expect(requests.length===2&&requests.every(row=>row.url==="/api/external-packaging-purchases/pending-receipts"),"package loader used the wrong endpoint or request count");
  requests[0].resolve({{data:{{purchase_orders:[{{id:1,supplier_name:"stale",items:[]}}]}}}});
  expect(await old===false,"stale package response reported success");
  expect(vm.externalIncomingPending[0].id===90,"stale package response replaced last-good data");
  expect(vm.externalIncomingLoading===true,"stale package response closed the latest loading state");

  requests[1].resolve({{data:{{purchase_orders:[
    {{id:2,supplier_name:"苏州包材",purchase_number:"EP-2",customer_name:"昆山华诚",order_number:"TM2",items:[{{purchase_item_id:21,product_name:"EPE",remaining_quantity:40,purchase_unit:"根"}}]}},
    {{id:3,supplier_name:"太仓配件",purchase_number:"EP-3",customer_name:"苏州思迈尔",order_number:"TM3",items:[{{purchase_item_id:31,product_name:"护角",remaining_quantity:60,purchase_unit:"只"}}]}},
  ]}}}});
  expect(await latest===true,"latest package request failed");
  expect(vm.externalIncomingPending.length===2&&vm.externalIncomingPending[0].id===2,"latest package rows were not applied");
  expect(vm.externalIncomingDrafts["2:21"]===40,"native-unit receipt draft was not rebuilt");
  expect(vm.externalIncomingLoading===false&&vm.externalIncomingLoaded===true,"latest package request did not settle its local state");

  vm.externalIncomingFilter="昆山";
  expect(vm.filteredExternalIncoming.length===1&&vm.filteredExternalIncoming[0].id===2,"package keyword filter leaked or missed rows");
  vm.externalIncomingFilter="";vm.pages.externalIncoming=2;
  expect(vm.pagedExternalIncoming.length===1&&vm.pagedExternalIncoming[0].id===3,"package pager did not use its independent page");

  const lastGood=vm.externalIncomingPending;
  const failed=vm.loadExternalIncoming();
  requests[2].reject(new Error("network down"));
  expect(await failed===false,"failed package request reported success");
  expect(vm.externalIncomingPending===lastGood,"failed package request discarded last-good rows");
  expect(vm.externalIncomingError.includes("network down"),"failed package request did not expose retry state");

  const retry=vm.loadExternalIncoming();
  requests[3].resolve({{data:{{purchase_orders:[]}}}});
  expect(await retry===true,"package retry did not recover");
  expect(vm.externalIncomingLoaded&&vm.externalIncomingPending.length===0&&vm.pages.externalIncoming===1,"empty retry did not publish the package empty state and clamp its page");
  expect(vm.marks.every(key=>key==="incoming:external-packaging")&&vm.invalidations.every(key=>key==="incoming:external-packaging"),"package request touched the paper cache key");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55b-package-race.js")


def test_permission_loss_and_auth_reset_clear_both_incoming_workspaces(
    tmp_path: Path,
) -> None:
    is_current = _method_body(
        "externalIncomingRequestIsCurrent(controller, authGeneration, userId) {",
        "async openExternalPackagingIncoming() {",
    )
    load_external = _method_body(
        "async loadExternalIncoming() {",
        "async receiveExternalPurchase(purchase) {",
    )
    mounted = _method_body("async mounted() {", "methods: {")
    reset = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")

    assert "window.erpAuthRequired = () =>" in mounted
    assert "this.authGeneration += 1;" in mounted
    assert "this.resetPagePerformanceState();" in mounted
    assert 'this.incomingWorkspace = "board";' in reset
    for token in (
        "this.incomingPending = [];",
        "this.incomingSelected = {};",
        "this.incomingReceiveAttempts = {};",
        "this.externalIncomingPending = [];",
        "this.externalIncomingDrafts = {};",
        'this.externalIncomingFilter = "";',
        "this.externalIncomingLoaded = false;",
        "this.pages.externalIncoming = 1;",
    ):
        assert token in reset

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
global.latestRequestControllers=new Map();
global.axios={{get:()=>Promise.reject({{response:{{status:403}}}})}};
const vm={{
  authGeneration:1,user:{{id:5}},activePage:"incoming",incomingWorkspace:"external-packaging",
  externalIncomingLoading:false,externalIncomingError:"old",resetCount:0,modalResetCount:0,
  beginLatestRequest(key){{const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},invalidatePageCache(){{}},markPageCache(){{throw new Error("403 was cached");}},
  resetPagePerformanceState(){{this.resetCount+=1;this.incomingWorkspace="board";this.externalIncomingLoading=false;this.externalIncomingError="";}},
  resetModalA11ySession(){{this.modalResetCount+=1;}},errorMessage(error){{return String(error);}},
  syncDesktopWorkspaceUrl(page){{this.syncedPage=page;}},
}};
vm.externalIncomingRequestIsCurrent=new FunctionCtor("controller","authGeneration","userId",{json.dumps(is_current, ensure_ascii=False)}).bind(vm);
vm.loadExternalIncoming=new AsyncFunction({json.dumps(load_external, ensure_ascii=False)}).bind(vm);
(async()=>{{
  expect(await vm.loadExternalIncoming()===false,"403 package request reported success");
  expect(vm.resetCount===1&&vm.modalResetCount===1,"permission loss did not clear both workspaces and modal draft");
  expect(vm.incomingWorkspace==="board"&&!vm.externalIncomingLoading,"permission loss retained package UI state");
  expect(vm.syncedPage==="incoming","GET 403 did not return the incoming URL to its paper main view");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55b-permission-reset.js")


def test_external_receipt_reuses_existing_api_and_clamps_only_package_page(
    tmp_path: Path,
) -> None:
    draft_key = _method_body(
        "externalIncomingDraftKey(purchaseId, purchaseItemId) {",
        "applyExternalIncomingOverview(",
    )
    filtered = _method_body("filteredExternalIncoming() {", "pagedExternalIncoming() {")
    apply_overview = _method_body(
        "applyExternalIncomingOverview(data) {",
        "externalIncomingRequestIsCurrent(",
    )
    receive = _method_body(
        "async receiveExternalPurchase(purchase) {",
        "async loadIncomingReceived",
    )

    page = INDEX.split(
        '<template v-else-if="activePage === \'incoming\'">', 1
    )[1].split('<template v-else-if="activePage === \'production\'">', 1)[0]
    assert 'v-if="hasPermission(\'incoming.execute\')"' in page
    assert 'v-else>{{ line.remaining_quantity }}</strong>' in page
    assert '{{ line.purchase_unit }}' in page
    assert 'axios.get("/api/external-packaging-purchases/pending-receipts"' in INDEX
    assert "axios.post(`/api/external-packaging-purchases/${purchase.id}/receipts`" in receive

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
global.createIdempotencyKey=()=>"p1-55b-fixed-key";
const requests=[];
global.axios={{post:async(url,payload)=>{{requests.push({{url,payload}});return {{data:{{created:true,overview:{{purchase_orders:[
  {{id:5,supplier_name:"苏州包材",items:[{{purchase_item_id:11,remaining_quantity:60,purchase_unit:"根"}}]}}
]}}}}}};}}}};
const vm={{
  authGeneration:2,user:{{id:8}},activePage:"incoming",incomingWorkspace:"external-packaging",
  pageSize:1,pages:{{externalIncoming:3}},externalIncomingFilter:"",externalIncomingSavingId:null,
  externalIncomingPending:[{{id:5,items:[{{purchase_item_id:11,remaining_quantity:100,purchase_unit:"根"}}]}}],
  externalIncomingDrafts:{{"5:11":40}},paperLoads:0,kpiLoads:0,orderLoads:0,toasts:[],refreshFails:false,invalidated:[],
  loadIncoming(){{this.paperLoads+=1;}},loadKpi(){{this.kpiLoads+=1;return Promise.resolve(true);}},
  loadOrders(){{this.orderLoads+=1;return this.refreshFails?Promise.reject(new Error("orders refresh failed")):Promise.resolve(true);}},
  markPageCache(key){{this.markedCache=key;}},
  invalidatePageCache(key){{this.invalidated.push(key);}},
  showToast(message,isError){{this.toasts.push({{message:String(message),isError:!!isError}});}},
  errorMessage(error){{return String(error?.message||error);}},
}};
vm.externalIncomingDraftKey=new FunctionCtor("purchaseId","purchaseItemId",{json.dumps(draft_key, ensure_ascii=False)}).bind(vm);
vm.applyExternalIncomingOverview=new FunctionCtor("data",{json.dumps(apply_overview, ensure_ascii=False)}).bind(vm);
Object.defineProperty(vm,"filteredExternalIncoming",{{get:new FunctionCtor({json.dumps(filtered, ensure_ascii=False)}).bind(vm)}});
vm.receiveExternalPurchase=new AsyncFunction("purchase",{json.dumps(receive, ensure_ascii=False)}).bind(vm);
(async()=>{{
  const purchase=vm.externalIncomingPending[0];
  expect(await vm.receiveExternalPurchase(purchase)===true,"package receipt failed");
  expect(requests.length===1&&requests[0].url==="/api/external-packaging-purchases/5/receipts","package receipt did not reuse the existing endpoint");
  expect(requests[0].payload.idempotency_key==="p1-55b-fixed-key","package receipt dropped the idempotency key");
  expect(requests[0].payload.lines.length===1&&requests[0].payload.lines[0].received_quantity===40,"package receipt changed the frozen native-unit quantity");
  expect(vm.pages.externalIncoming===1,"package write did not clamp its own depleted page");
  expect(vm.markedCache==="incoming:external-packaging","package write marked the paper cache");
  expect(vm.paperLoads===0,"package write reloaded or mutated the paper page");
  expect(vm.kpiLoads===1&&vm.orderLoads===1,"existing package write projections were not refreshed");
  expect(vm.externalIncomingPending[0].items[0].purchase_unit==="根","package overview lost the native purchase unit");

  vm.refreshFails=true;
  const secondPurchase=vm.externalIncomingPending[0];
  expect(await vm.receiveExternalPurchase(secondPurchase)===true,"committed package receipt was misreported when a related refresh failed");
  expect(requests.length===2,"refresh failure retried or skipped the package write unexpectedly");
  expect(vm.toasts.some(row=>row.message.includes("包材收料已经保存")&&row.message.includes("刷新失败")),"committed write did not expose the refresh-only warning");
  expect(!vm.toasts.some(row=>row.message.includes("外购包装收料失败")),"refresh failure was mislabeled as a receipt write failure");
  expect(vm.invalidated.length===2&&vm.invalidated.every(key=>key==="incoming:external-packaging"),"package POST touched or skipped the wrong cache key");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55b-receipt-clamp.js")


def test_external_receipt_403_clears_session_even_after_navigating_away(
    tmp_path: Path,
) -> None:
    draft_key = _method_body(
        "externalIncomingDraftKey(purchaseId, purchaseItemId) {",
        "applyExternalIncomingOverview(",
    )
    receive = _method_body(
        "async receiveExternalPurchase(purchase) {",
        "async loadIncomingReceived",
    )

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
global.createIdempotencyKey=()=>"permission-attempt";
let rejectWrite;
global.axios={{post:()=>new Promise((_resolve,reject)=>{{rejectWrite=reject;}})}};
const purchase={{id:8,items:[{{purchase_item_id:81,remaining_quantity:12,purchase_unit:"只"}}]}};
const vm={{
  authGeneration:7,user:{{id:22}},activePage:"incoming",incomingWorkspace:"external-packaging",
  incomingPending:[{{item_id:"paper-secret"}}],incomingSelected:{{"paper-secret":true}},
  externalIncomingPending:[purchase],externalIncomingDrafts:{{"8:81":12}},
  externalIncomingSavingId:null,externalIncomingSavingAttemptId:"",resetCount:0,modalResetCount:0,
  invalidatePageCache(){{}},showToast(message){{throw new Error("unexpected toast: "+message);}},
  errorMessage(error){{return String(error?.message||error);}},
  resetPagePerformanceState(){{
    this.resetCount+=1;this.incomingWorkspace="board";this.incomingPending=[];this.incomingSelected={{}};
    this.externalIncomingPending=[];this.externalIncomingDrafts={{}};this.externalIncomingSavingId=null;this.externalIncomingSavingAttemptId="";
  }},
  resetModalA11ySession(){{this.modalResetCount+=1;}},syncDesktopWorkspaceUrl(page){{this.syncedPage=page;}},
}};
vm.externalIncomingDraftKey=new FunctionCtor("purchaseId","purchaseItemId",{json.dumps(draft_key, ensure_ascii=False)}).bind(vm);
vm.receiveExternalPurchase=new AsyncFunction("purchase",{json.dumps(receive, ensure_ascii=False)}).bind(vm);
(async()=>{{
  const pending=vm.receiveExternalPurchase(purchase);
  expect(typeof rejectWrite==="function","package receipt POST did not start");
  vm.activePage="orders";vm.incomingWorkspace="board";
  rejectWrite({{response:{{status:403}}}});
  expect(await pending===false,"403 package receipt reported success");
  expect(vm.resetCount===1&&vm.modalResetCount===1,"late 403 after navigation did not reset the active session");
  expect(vm.syncedPage==="orders","late POST 403 rewrote the address bar back to incoming");
  expect(!vm.incomingPending.length&&!Object.keys(vm.incomingSelected).length,"late 403 retained paper account data");
  expect(!vm.externalIncomingPending.length&&!Object.keys(vm.externalIncomingDrafts).length,"late 403 retained package account data or draft");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55b-receipt-403.js")

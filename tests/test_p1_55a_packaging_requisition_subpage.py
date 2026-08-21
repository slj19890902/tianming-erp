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
    assert node is not None, "Node.js is required for P1-55A frontend regressions"
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


def test_board_and_external_packaging_are_distinct_requisition_views() -> None:
    requisition = INDEX.split(
        '<template v-else-if="activePage === \'requisition\'">', 1
    )[1].split('<template v-else-if="activePage === \'incoming\'">', 1)[0]
    external, board = requisition.split("<template v-else>", 1)

    assert "requisitionWorkspace==='external-packaging'" in external
    assert "<h2>包材报料</h2>" in external
    assert '@click="returnToBoardRequisition">返回纸板报料</button>' in external
    assert "pagedExternalPurchaseRouting" in external
    assert "核对并确认采购" in external
    assert "请管理员在成本权限下确认采购" in external
    assert "合并报料" not in external
    assert "暂不报料" not in external
    assert "库存补库" not in external

    assert "<h2>纸板报料</h2>" in board
    assert '@click="openExternalPackagingRequisition">包材报料</button>' in board
    assert "external-route-" not in board
    assert "pagedExternalPurchaseRouting" not in board
    assert "合并报料" in board
    assert "暂不报料" in board
    assert "库存补库" in board


def test_package_subpage_exposes_purchase_history_print_and_cancel_operations() -> None:
    requisition = INDEX.split(
        '<template v-else-if="activePage === \'requisition\'">', 1
    )[1].split('<template v-else-if="activePage === \'incoming\'">', 1)[0]
    external = requisition.split("<template v-else>", 1)[0]
    history_loader = _method_body("loadExternalPurchaseHistory")
    switcher = _method_body("selectExternalPurchaseView")

    assert "待确认采购" in external
    assert "采购历史" in external
    assert "已确认的采购不会留在待确认列表" in external
    assert "查看 / 撤销采购" in external
    assert "查看采购" in external
    assert "打印采购单" in external
    assert "purchase.received_quantity" in external
    assert "purchase.cancellation.reason" in external
    assert "/api/external-packaging-purchases/history" in history_loader
    assert 'this.externalPurchaseView = target;' in switcher
    assert "this.loadExternalPurchaseHistory()" in switcher
    assert "!this.canAdmin || !this.canViewCosts" in switcher


def test_refreshable_subpage_url_restores_external_packaging_and_return_clears_it(
    tmp_path: Path,
) -> None:
    initial = _method_body("initialRequisitionWorkspaceFromLocation")
    sync = _method_body("syncDesktopWorkspaceUrl")
    script = f"""
const FunctionCtor=Function;
global.window={{
  location:{{href:"http://erp.local/?page=requisition&subpage=external-packaging",search:"?page=requisition&subpage=external-packaging",pathname:"/",hash:""}},
  history:{{state:{{marker:1}},calls:[],replaceState(state,title,url){{this.calls.push(url);const parsed=new URL(url,"http://erp.local");window.location.href=parsed.href;window.location.search=parsed.search;window.location.pathname=parsed.pathname;window.location.hash=parsed.hash;}}}},
}};
const vm={{}};
vm.initialRequisitionWorkspaceFromLocation=new FunctionCtor("page",{json.dumps(initial, ensure_ascii=False)}).bind(vm);
vm.syncDesktopWorkspaceUrl=new FunctionCtor("page","subpage=''",{json.dumps(sync, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
expect(vm.initialRequisitionWorkspaceFromLocation("requisition")==="external-packaging","refresh did not restore package subpage");
expect(vm.initialRequisitionWorkspaceFromLocation("incoming")==="board","package query leaked to another page");
vm.syncDesktopWorkspaceUrl("requisition", "external-packaging");
expect(window.history.calls.at(-1)==="/?page=requisition&subpage=external-packaging","package URL was not recoverable");
vm.syncDesktopWorkspaceUrl("requisition");
expect(window.history.calls.at(-1)==="/?page=requisition","return did not clear package subpage");
window.location.search="?page=requisition";
expect(vm.initialRequisitionWorkspaceFromLocation("requisition")==="board","plain requisition URL did not default to board");
"""
    _run_node(script, tmp_path, "p1-55a-requisition-url.js")


def test_board_loader_makes_zero_full_external_confirmation_requests(
    tmp_path: Path,
) -> None:
    load = _method_body("loadRequisition")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const calls=[];
global.axios={{get:async(url,options)=>{{calls.push({{url,options}});if(url.includes("/holds"))return {{data:{{items:[],total:0}}}};if(url.includes("/pending"))return {{data:{{items:[],total:0,page:1,supplier_counts:[]}}}};throw new Error(`unexpected ${{url}}`);}}}};
const controller={{signal:{{}}}};
const vm={{
  activePage:"requisition",requisitionWorkspace:"board",authGeneration:3,user:{{id:7}},pageSize:25,
  pages:{{requisitionPending:1}},requisitionSupplierFilter:"",requisitionPendingLoading:false,requisitionPendingError:"",
  requisitionPendingRetryPage:1,requisitionPendingRetrySupplierFilter:"",requisitionPendingRetryScope:"",
  beginLatestRequest(){{return controller;}},finishLatestRequest(){{}},invalidatePageCache(){{}},markPageCache(){{}},
  pendingRequisitionRequestIsCurrent(){{return true;}},isCancelledRequest(){{return false;}},errorMessage(error){{return error.message;}},
  async autoReleaseReadyRequisitionHolds(){{return [];}},requisitionPendingRequestParams(){{return {{page:1,page_size:25}};}},
  applyRequisitionPendingResponse(){{}},applyRequisitionHoldsResponse(){{}},
}};
vm.loadRequisition=new AsyncFunction("{{skipAutoRelease=false}}={{}}",{json.dumps(load, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.loadRequisition()===true,"board workbench did not load successfully");
  expect(calls.some(call=>call.url==="/api/requisition/pending"),"board pending route was not loaded");
  expect(calls.filter(call=>call.url==="/api/external-packaging-purchases/pending-confirmations").length===0,"board first paint downloaded full package details");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55a-board-zero-external.js")


def test_package_open_is_on_demand_and_return_preserves_board_selection(
    tmp_path: Path,
) -> None:
    open_external = _method_body("openExternalPackagingRequisition")
    return_board = _method_body("returnToBoardRequisition")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const calls=[];
const vm={{
  activePage:"requisition",requisitionWorkspace:"board",externalPurchaseRoutingLoaded:false,
  requisitionSelected:{{"17":true}},selectedPendingKeys:["item:17"],pages:{{externalPurchaseRouting:1}},requisitionPendingLoading:false,
  pageAllowed(page){{return page==="requisition";}},showToast(){{}},
  syncDesktopWorkspaceUrl(page,subpage=""){{calls.push(`url:${{page}}:${{subpage}}`);}},
  async loadExternalPurchaseRouting(){{calls.push("load");return true;}},
  cancelLatestRequest(key){{calls.push(`cancel:${{key}}`);}},
  pageCacheFresh(){{return true;}},async loadRequisition(){{calls.push("board-load");return true;}},
}};
vm.openExternalPackagingRequisition=new AsyncFunction({json.dumps(open_external, ensure_ascii=False)}).bind(vm);
vm.returnToBoardRequisition=new AsyncFunction({json.dumps(return_board, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(calls.filter(call=>call==="load").length===0,"package loaded before click");
  expect(await vm.openExternalPackagingRequisition()===true,"package click did not finish");
  expect(vm.requisitionWorkspace==="external-packaging","package click did not switch workspace");
  expect(calls.filter(call=>call==="load").length===1,"one click did not make exactly one package load");
  expect(calls.includes("url:requisition:external-packaging"),"package click did not persist URL");
  await vm.returnToBoardRequisition();
  expect(vm.requisitionWorkspace==="board","return did not restore board workspace");
  expect(calls.includes("url:requisition:"),"return did not clear package URL");
  expect(vm.requisitionSelected["17"]===true&&vm.selectedPendingKeys[0]==="item:17","package navigation polluted board selection");
  expect(calls.filter(call=>call==="load").length===1,"return triggered an unrelated package request");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55a-package-open-return.js")


def test_package_loader_is_latest_wins_keeps_last_good_and_retries(
    tmp_path: Path,
) -> None:
    load = _method_body("loadExternalPurchaseRouting")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const latest=new Map();const pending=[];const toasts=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  activePage:"requisition",requisitionWorkspace:"external-packaging",authGeneration:4,user:{{id:8}},
  externalPurchaseRouting:[{{order_item_id:"last-good"}}],externalPurchaseRoutingLoading:false,
  externalPurchaseRoutingError:"",externalPurchaseRoutingLoaded:true,pages:{{externalPurchaseRouting:2}},pageSize:25,
  beginLatestRequest(key){{latest.get(key)?.abort();const controller=new AbortController();latest.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latest.get(key)===controller)latest.delete(key);}},
  externalPurchaseRoutingRequestIsCurrent(controller,authGeneration,userId){{return latest.get("requisition:external-packaging")===controller&&this.authGeneration===authGeneration&&this.user?.id===userId&&this.activePage==="requisition"&&this.requisitionWorkspace==="external-packaging";}},
  isExternalPurchaseRoutingRequestCurrent(controller,authGeneration,userId){{return this.externalPurchaseRoutingRequestIsCurrent(controller,authGeneration,userId);}},
  isCancelledRequest(error){{return error?.name==="AbortError"||error?.code==="ERR_CANCELED";}},
  showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},errorMessage(error){{return error.message;}},
  markPageCache(){{}},invalidatePageCache(){{}},clearPendingRequisitionSelection(){{}},
}};
Object.defineProperty(vm,"filteredExternalPurchaseRouting",{{get(){{return vm.externalPurchaseRouting;}}}});
vm.loadExternalPurchaseRouting=new AsyncFunction({json.dumps(load, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const old=vm.loadExternalPurchaseRouting();
  const newest=vm.loadExternalPurchaseRouting();
  expect(pending.length===2,"rapid package reload did not start two generations");
  expect(pending.every(row=>row.url==="/api/external-packaging-purchases/pending-confirmations"),"existing package endpoint was not reused");
  pending[0].resolve({{data:{{items:[{{order_item_id:"stale"}}]}}}});
  expect(await old===false,"stale response reported success");
  expect(vm.externalPurchaseRouting[0].order_item_id==="last-good","stale response replaced last-good data");
  expect(vm.externalPurchaseRoutingLoading===true,"stale response closed latest loading state");
  pending[1].resolve({{data:{{items:[{{order_item_id:"fresh",purchase_unit:"卷"}}]}}}});
  expect(await newest===true,"latest package response did not succeed");
  expect(vm.externalPurchaseRouting[0].order_item_id==="fresh","latest response was not applied");
  expect(vm.externalPurchaseRoutingLoaded===true&&vm.externalPurchaseRoutingLoading===false,"success state is incomplete");

  const failed=vm.loadExternalPurchaseRouting();
  pending[2].reject(new Error("network down"));
  expect(await failed===false,"failed package request reported success");
  expect(vm.externalPurchaseRouting[0].order_item_id==="fresh","failure discarded last-good rows");
  expect(vm.externalPurchaseRoutingError.includes("network down"),"failure did not expose retry error");
  const retried=vm.loadExternalPurchaseRouting();
  pending[3].resolve({{data:{{items:[{{order_item_id:"recovered"}}]}}}});
  expect(await retried===true&&vm.externalPurchaseRouting[0].order_item_id==="recovered","retry did not recover");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55a-package-latest-wins.js")


def test_logout_401_and_account_boundary_clear_package_cache_and_drafts() -> None:
    reset = _method_body("resetPagePerformanceState")
    logout = _method_body("logout")
    mounted_401 = INDEX.split("window.erpAuthRequired = () => {", 1)[1].split("};", 1)[0]

    for marker in (
        'this.requisitionWorkspace = "board";',
        "this.externalPurchaseRouting = [];",
        "this.externalPurchaseRoutingLoading = false;",
        'this.externalPurchaseRoutingError = "";',
        'this.externalPurchaseRoutingFilter = "";',
        "this.externalPurchaseRoutingLoaded = false;",
        "this.pages.externalPurchaseRouting = 1;",
        "this.requisitionSelected = {};",
        "this.selectedPendingKeys = [];",
    ):
        assert marker in reset
    assert "this.authGeneration += 1;" in logout
    assert "this.resetPagePerformanceState();" in logout
    assert "this.authGeneration += 1;" in mounted_401
    assert "this.resetPagePerformanceState();" in mounted_401


def test_existing_purchase_confirmation_permission_and_endpoint_contract_remain() -> None:
    requisition = INDEX.split(
        '<template v-else-if="activePage === \'requisition\'">', 1
    )[1].split('<template v-else-if="activePage === \'incoming\'">', 1)[0]
    external = requisition.split("<template v-else>", 1)[0]

    assert 'v-if="canAdmin && canViewCosts"' in external
    assert '@click="openExternalPurchase(row)"' in external
    assert "purchase_unit" in external
    assert "/api/external-packaging-purchases/pending-confirmations" in _method_body(
        "loadExternalPurchaseRouting"
    )
    assert "/external-packaging-purchase/confirm" in INDEX
    assert "idempotency_key" in INDEX
    assert "价格版本 {{ selectedExternalPurchaseCandidate(row).price.version_number }}" in INDEX


def test_direct_external_first_load_does_not_freshen_board_cache_and_return_loads_board(
    tmp_path: Path,
) -> None:
    load_page = _method_body("loadPage")
    return_board = _method_body("returnToBoardRequisition")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const fresh=new Set();const calls=[];
const vm={{
  pageLoadSequence:0,authGeneration:2,user:{{id:11}},activePage:"requisition",requisitionWorkspace:"external-packaging",loading:false,
  currentPageSearchGeneration(){{return 0;}},pageCacheFresh(key){{return fresh.has(key);}},
  invalidatePageCache(key){{fresh.delete(key);}},markPageCache(key){{fresh.add(key);}},
  async loadExternalPurchaseRouting(){{calls.push("external");return true;}},
  async loadRequisition(options){{calls.push(`board:${{options?.skipAutoRelease===true}}`);return true;}},
  cancelLatestRequest(key){{calls.push(`cancel:${{key}}`);}},syncDesktopWorkspaceUrl(page,subpage=""){{calls.push(`url:${{page}}:${{subpage}}`);}},
  showToast(message){{throw new Error(`unexpected toast: ${{message}}`);}},errorMessage(error){{return error.message;}},
}};
vm.loadPage=new AsyncFunction("page","{{force=false,supplierPromise=null}}={{}}",{json.dumps(load_page, ensure_ascii=False)}).bind(vm);
vm.returnToBoardRequisition=new AsyncFunction({json.dumps(return_board, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.loadPage("requisition")===true,"direct package load failed");
  expect(calls.filter(row=>row==="external").length===1,"direct package URL did not load package once");
  expect(fresh.has("requisition:external-packaging"),"package resource cache was not marked");
  expect(!fresh.has("requisition"),"package resource incorrectly freshened board cache");
  expect(await vm.returnToBoardRequisition()===true,"return to board failed");
  expect(calls.filter(row=>row==="board:true").length===1,"return reused package cache instead of loading board");
  expect(vm.requisitionWorkspace==="board","return did not restore board workspace");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55a-direct-package-cache-isolation.js")


def test_pending_confirmations_403_clears_modal_filters_pages_selections_and_caches(
    tmp_path: Path,
) -> None:
    reset = _method_body("resetPagePerformanceState")
    current = _method_body("externalPurchaseRoutingRequestIsCurrent")
    load = _method_body("loadExternalPurchaseRouting")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
global.latestRequestControllers=new Map();
global.searchDebounceTimers=new Map();global.pageSearchGenerations=new Map();global.pinyinSearchTextCache=new Map();
global.today=()=>"2026-08-14";global.plusDays=()=>"2026-08-21";
global.blankReceiptReminderEditor=()=>({{}});
global.axios={{get:async()=>{{throw Object.assign(new Error("forbidden"),{{response:{{status:403}}}});}}}};
const vm={{
  authGeneration:5,user:{{id:22}},activePage:"requisition",requisitionWorkspace:"external-packaging",
  pageLoadSequence:3,loginAttemptSequence:4,deliveryDetailRequestSequence:1,pages:{{requisitionPending:2,requisitionHolds:3,requisitionReported:2,externalPurchaseRouting:4,incomingPending:5}},
  pageCacheUpdatedAt:{{requisition:101,"requisition:external-packaging":202}},
  requisitionPending:[{{item_id:1}}],requisitionHolds:[{{id:2}}],requisitionSelected:{{"1":true}},selectedPendingKeys:["item:1"],selectedBomSnapshotIds:[9],
  externalPurchaseRouting:[{{order_item_id:8}}],externalPurchaseRoutingFilter:"华诚",externalPurchaseRoutingLoaded:true,externalPurchaseRoutingError:"old",
  externalPurchase:{{loading:false,saving:false,error:"",submitError:"",status:"review",orderId:8,orderLabel:"SO-8",items:[{{id:3}}],confirmation:null,idempotencyKey:"secret-key"}},
  modal:{{type:"externalPurchase",title:"外购包装采购核对"}},
  beginLatestRequest(key){{const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},errorMessage(error){{return error.message;}},markPageCache(){{}},
  cancelOrderGroupDetailRequests(){{}},resetModalA11ySession(options){{if(options?.discardModal)this.modal=null;}},
}};
vm.resetPagePerformanceState=new FunctionCtor({json.dumps(reset, ensure_ascii=False)}).bind(vm);
vm.externalPurchaseRoutingRequestIsCurrent=new FunctionCtor("controller","authGeneration","userId",{json.dumps(current, ensure_ascii=False)}).bind(vm);
vm.loadExternalPurchaseRouting=new AsyncFunction({json.dumps(load, ensure_ascii=False)}).bind(vm);
Object.defineProperty(vm,"filteredExternalPurchaseRouting",{{get(){{return vm.externalPurchaseRouting;}}}});
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.loadExternalPurchaseRouting()===false,"403 package request reported success");
  expect(vm.externalPurchaseRouting.length===0&&vm.externalPurchaseRoutingLoaded===false,"403 retained package cache");
  expect(vm.externalPurchaseRoutingFilter===""&&vm.pages.externalPurchaseRouting===1,"403 retained package filter/page");
  expect(vm.requisitionPending.length===0&&vm.requisitionHolds.length===0,"403 retained board cache");
  expect(Object.keys(vm.requisitionSelected).length===0&&vm.selectedPendingKeys.length===0&&vm.selectedBomSnapshotIds.length===0,"403 retained board selection");
  expect(vm.externalPurchase.orderId===null&&vm.externalPurchase.items.length===0&&vm.externalPurchase.idempotencyKey==="","403 retained purchase modal draft");
  expect(vm.modal===null,"403 did not discard the open purchase modal");
  expect(Object.keys(vm.pageCacheUpdatedAt).length===0,"403 retained cross-account cache timestamps");
  expect(vm.requisitionWorkspace==="board","403 did not restore safe board workspace");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-55a-package-403-reset.js")

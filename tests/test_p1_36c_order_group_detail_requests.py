from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX
    assert next_signature in INDEX
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    target = tmp_path / "p1-36c-order-detail-requests.js"
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


def test_order_group_detail_shares_inflight_request_and_latest_modal_intent_wins(
    tmp_path: Path,
) -> None:
    ensure = _method_body(
        "async ensureOrderGroupDetail(group, {force=false}={}) {",
        "async toggleOrderGroup(group) {",
    )
    group_detail = _method_body(
        "async openOrderGroupDetail(group) {", "async openCostGaps() {"
    )
    editor = _method_body(
        "async openOrderEditor(group) {", "async dangerRollbackOrderGroup() {"
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.orderGroupDetailRequests=new Map();global.orderGroupDetailRequestSequence=0;
global.latestRequestControllers=new Map();const pending=[];const toasts=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const ensure=new AsyncFunction("group","{{force=false}}={{}}",{json.dumps(ensure, ensure_ascii=False)});
const openDetail=new AsyncFunction("group",{json.dumps(group_detail, ensure_ascii=False)});
const openEditor=new AsyncFunction("group",{json.dumps(editor, ensure_ascii=False)});
const vm={{
  user:{{id:1}},filters:{{orderScope:"active"}},orderListGeneration:4,orderGroupDetails:{{}},orderGroupDetailLoading:{{}},orderGroupDetailErrors:{{}},
  modal:null,orderGroupDetail:null,orderEditForm:null,orderEditGroup:null,showOrderEditDanger:false,
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  cancelOrderTraceChildRequests(){{}},isCancelledRequest(error){{return error?.name==="AbortError";}},
  errorMessage(error){{return error?.message||String(error);}},showToast(message,isError){{toasts.push({{message,isError}});}}
}};
vm.ensureOrderGroupDetail=ensure.bind(vm);vm.openOrderGroupDetail=openDetail.bind(vm);vm.openOrderEditor=openEditor.bind(vm);
const group={{key:"1|PO-1",customer_id:1,customer_name:"客户甲",customer_po:"PO-1",orders:[{{id:11}}]}};
const payload={{customer_id:1,customer_name:"客户甲",customer_po:"PO-1",orders:[{{id:11,order_number:"TM-11",order_date:"2026-08-10",delivery_date:"2026-08-11",remark:"",items:[]}}]}};
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const detailIntent=vm.openOrderGroupDetail(group);
  const editIntent=vm.openOrderEditor(group);
  expect(pending.length===1,"same group started duplicate requests");
  pending[0].resolve({{data:payload}});
  expect(await detailIntent===false,"older modal intent was accepted");
  expect(await editIntent===true&&vm.modal?.type==="orderEdit","latest editor intent did not open");
  expect(vm.orderGroupDetails[group.key]===payload,"shared detail was not cached");
  const cached=await vm.ensureOrderGroupDetail(group);
  expect(cached===payload&&pending.length===1,"cached detail started another request");
  expect(toasts.length===0,"successful shared request leaked an error");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_order_group_force_retry_rejects_the_stale_response(tmp_path: Path) -> None:
    ensure = _method_body(
        "async ensureOrderGroupDetail(group, {force=false}={}) {",
        "async toggleOrderGroup(group) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.orderGroupDetailRequests=new Map();global.orderGroupDetailRequestSequence=0;const pending=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{filters:{{orderScope:"active"}},orderListGeneration:2,orderGroupDetails:{{}},orderGroupDetailLoading:{{}},orderGroupDetailErrors:{{}},
  isCancelledRequest(error){{return error?.name==="AbortError";}},errorMessage(error){{return error?.message||String(error);}},showToast(){{throw new Error("stale request showed an error");}}}};
vm.ensureOrderGroupDetail=new AsyncFunction("group","{{force=false}}={{}}",{json.dumps(ensure, ensure_ascii=False)}).bind(vm);
const group={{key:"2|PO-2",customer_id:2,customer_po:"PO-2",orders:[{{id:22}}]}};
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const oldRequest=vm.ensureOrderGroupDetail(group);
  const retry=vm.ensureOrderGroupDetail(group,{{force:true}});
  expect(pending.length===2&&pending[0].options.signal.aborted,"force retry did not replace the old request");
  pending[0].resolve({{data:{{orders:[{{id:1}}]}}}});pending[1].resolve({{data:{{orders:[{{id:2}}]}}}});
  expect(await oldRequest===null,"stale response was accepted");
  const latest=await retry;
  expect(latest.orders[0].id===2&&vm.orderGroupDetails[group.key].orders[0].id===2,"latest retry did not own the cache");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_order_list_applies_only_the_latest_response_and_clears_group_requests() -> None:
    loader = _method_body("async loadOrders() {", "async autoReleaseReadyRequisitionHolds")
    assert 'latestRequestControllers.get("orders:list") !== controller' in loader
    assert "this.cancelOrderGroupDetailRequests()" in loader
    assert "this.orderListGeneration += 1" in loader
    assert loader.index("this.cancelOrderGroupDetailRequests()") < loader.index(
        "this.orderListGeneration += 1"
    )


def test_order_list_stale_response_cannot_clear_latest_page_state(tmp_path: Path) -> None:
    loader = _method_body("async loadOrders() {", "async autoReleaseReadyRequisitionHolds")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{activePage:"orders",pages:{{orders:1}},pageSize:25,filters:{{orderScope:"active"}},orders:[{{id:99}}],
  ordersTotal:1,ordersUnfinishedTotal:1,orderListGeneration:0,expandedOrders:{{old:true}},orderGroupDetails:{{old:{{}}}},
  orderGroupDetailLoading:{{}},orderGroupDetailErrors:{{}},orderBomDemandSaveState:null,cancelCount:0,
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="AbortError";}},cancelOrderGroupDetailRequests(){{this.cancelCount+=1;}},
  resetOrderBomDemandSaveState(){{throw new Error("unexpected reset");}}
}};
vm.loadOrders=new AsyncFunction({json.dumps(loader, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const oldPage=vm.loadOrders();vm.pages.orders=2;const latestPage=vm.loadOrders();
  pending[1].resolve({{data:{{items:[{{id:2}}],total:2,unfinished_total:2}}}});
  expect(await latestPage===true,"latest order page did not complete");
  pending[0].resolve({{data:{{items:[{{id:1}}],total:1,unfinished_total:1}}}});
  expect(await oldPage===false,"stale order response was accepted");
  expect(vm.orders[0].id===2&&vm.ordersTotal===2,"stale response replaced latest rows");
  expect(vm.orderListGeneration===1&&vm.cancelCount===1,"stale response invalidated current detail state");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_trace_stays_explicit_and_is_not_prefetched_by_group_detail() -> None:
    ensure = _method_body(
        "async ensureOrderGroupDetail(group, {force=false}={}) {",
        "async toggleOrderGroup(group) {",
    )
    trace = _method_body("async openOrderTrace(order, item) {", "traceCurrentEvent() {")
    assert "/documents" not in ensure
    assert "/items/${itemId}/documents" in trace
    assert "axios.get" in trace

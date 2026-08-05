from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX, f"missing Vue method: {signature}"
    assert next_signature in INDEX, f"missing Vue method boundary: {next_signature}"
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for order group regression"
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


def test_order_group_ui_has_dedicated_state_button_and_close_guards() -> None:
    assert (
        "orderGroupSaveState:{saving:false,committed:false,outcomeUncertain:false,"
        "result:null}"
    ) in INDEX
    assert "orderGroupSaveState.saving ? '正在保存订单组…'" in INDEX
    assert "orderGroupSaveState.committed ? '订单组已保存'" in INDEX
    assert "modal?.type==='orderEdit' && orderGroupSaveState.saving" in INDEX
    assert "订单组正在保存，请勿重复点击" in INDEX
    open_body = _method_body("async openOrderEditor(group) {", "async dangerRollbackOrderGroup() {")
    assert "this.orderGroupSaveState = {saving:false,committed:false,outcomeUncertain:false,result:null};" in open_body


def test_order_group_runtime_blocks_duplicate_and_freezes_payload(tmp_path: Path) -> None:
    body = _method_body("async saveOrderGroup(orderIds, orderPayload) {", "async saveModal() {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
let releases=[];const calls=[];
const axios={{put:(url,payload)=>new Promise(resolve=>{{calls.push({{url,payload}});releases.push(()=>resolve({{data:{{ok:true}}}}));}})}};
const factory=new Function("axios","return async function(orderIds,orderPayload) {{"+body+"}}");
const sourceIds=[11,12],source={{customer_po:"PO-1",delivery_date:"2026-08-08",remark:"原备注"}};
const vm={{
  orderGroupSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},
  closeCount:0,closeModal(){{this.closeCount+=1;}},loadOrders:async()=>true,
  showToast(){{}},errorMessage:error=>error.message,
}};
vm.saveOrderGroup=factory(axios).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveOrderGroup(sourceIds,source);
  const duplicate=await Promise.race([vm.saveOrderGroup(sourceIds,source),new Promise((_,reject)=>setTimeout(()=>reject(new Error("duplicate pending")),50))]);
  sourceIds[0]=99;source.customer_po="CHANGED";source.remark="CHANGED";
  expect(duplicate?._in_flight===true,"duplicate group save was not rejected");
  expect(calls.length===2&&calls[0].url==="/api/orders/11"&&calls[1].url==="/api/orders/12","frozen order ids were not used");
  expect(calls.every(call=>call.payload.customer_po==="PO-1"&&call.payload.remark==="原备注"),"group payload was not frozen");
  releases.forEach(release=>release());const result=await first;
  expect(result.succeeded===2&&vm.orderGroupSaveState.committed,"successful group was not committed");
  expect(vm.closeCount===1,"successful group did not close original modal");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-group-duplicate.js")


def test_order_group_refresh_failure_preserves_committed_success(tmp_path: Path) -> None:
    body = _method_body("async saveOrderGroup(orderIds, orderPayload) {", "async saveModal() {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const axios={{put:async()=>({{data:{{ok:true}}}})}};
const factory=new Function("axios","return async function(orderIds,orderPayload) {{"+body+"}}");
const toasts=[];let closed=false;
const vm={{
  orderGroupSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},
  closeModal(){{closed=true;}},loadOrders:async()=>{{throw new Error("orders offline");}},
  showToast:(message,isError)=>toasts.push({{message,isError}}),errorMessage:error=>error.message,
}};
(async()=>{{
  const result=await factory(axios).bind(vm)([11,12],{{customer_po:"PO-1"}});
  if(!closed||!vm.orderGroupSaveState.committed)throw new Error("committed group was not locked and closed");
  if(!result._refresh_failed)throw new Error("group refresh failure was not reported");
  if(toasts.length!==1||!toasts[0].message.includes("订单组已保存")||!toasts[0].message.includes("不要重复提交"))throw new Error("committed group truth was hidden");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-group-refresh.js")


def test_order_group_partial_unknown_and_explicit_failures(tmp_path: Path) -> None:
    body = _method_body("async saveOrderGroup(orderIds, orderPayload) {", "async saveModal() {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=axios=>new Function("axios","return async function(orderIds,orderPayload) {{"+body+"}}")(axios);
const makeVm=()=>({{
  orderGroupSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},closeCount:0,
  closeModal(){{this.closeCount+=1;}},loadOrders:async()=>true,showToast(){{}},errorMessage:error=>error.message,
}});
const responseError=message=>{{const error=new Error(message);error.response={{status:409}};return error;}};
(async()=>{{
  const partialVm=makeVm();let call=0;
  try{{await factory({{put:async()=>{{call+=1;if(call===2)throw responseError("conflict");return {{data:{{ok:true}}}};}}}}).bind(partialVm)([11,12],{{customer_po:"PO"}});throw new Error("partial did not throw");}}
  catch(error){{if(!error._orderGroupPartial||partialVm.closeCount!==1||!partialVm.orderGroupSaveState.committed)throw error;}}
  const unknownVm=makeVm();const networkError=new Error("network");
  try{{await factory({{put:async()=>{{throw networkError;}}}}).bind(unknownVm)([11],{{customer_po:"PO"}});throw new Error("unknown did not throw");}}
  catch(error){{if(!error._orderGroupOutcomeUncertain||unknownVm.closeCount!==1||!unknownVm.orderGroupSaveState.outcomeUncertain)throw error;}}
  const explicitVm=makeVm();const explicit=responseError("validation");
  try{{await factory({{put:async()=>{{throw explicit;}}}}).bind(explicitVm)([11],{{customer_po:"PO"}});throw new Error("explicit did not throw");}}
  catch(error){{
    if(error!==explicit)throw error;
    const state=explicitVm.orderGroupSaveState;
    if(explicitVm.closeCount!==0||state.saving||state.committed||state.outcomeUncertain)throw new Error("explicit failure did not keep form retryable");
  }}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-group-errors.js")


def test_save_modal_routes_order_group_results() -> None:
    body = _method_body("async saveModal() {", "async dispatchDelivery(row, options = {}) {")
    assert 'if (this.modal?.type === "orderEdit" && (this.orderGroupSaveState.saving' in body
    assert "const savedGroup = await this.saveOrderGroup(this.orderEditForm.order_ids, payload);" in body
    assert "if (savedGroup?._in_flight) return false;" in body
    assert "if (error?._orderGroupPartial)" in body
    assert "订单组部分保存" in body
    assert "if (error?._orderGroupOutcomeUncertain)" in body
    assert "结果暂不确定" in body

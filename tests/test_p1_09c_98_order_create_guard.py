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
    assert node is not None, "Node.js is required for order create regression"
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


def test_order_create_ui_has_dedicated_state_button_and_close_guards() -> None:
    assert (
        'orderCreateSaveState:{saving:false,committed:false,outcomeUncertain:false,'
        'result:null}'
    ) in INDEX
    assert "orderCreateSaveState.saving ? '正在保存订单…'" in INDEX
    assert "orderCreateSaveState.committed ? '订单已保存'" in INDEX
    assert "modal?.type==='order' && orderCreateSaveState.saving" in INDEX
    assert "订单正在保存，请勿重复点击" in INDEX
    assert "该订单已经保存，请刷新订单列表核对" in INDEX
    open_body = _method_body("async openOrder() {", "resetOrderCommonBoxPicker() {")
    assert "this.orderCreateSaveState = {saving:false,committed:false,outcomeUncertain:false,result:null};" in open_body


def test_order_create_runtime_blocks_duplicate_and_freezes_payload(tmp_path: Path) -> None:
    body = _method_body("async saveNewOrder(orderPayload) {", "async saveCurrentOrderItem(orderItemId, orderItemPayload) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
let release,postCount=0,closeCount=0;const calls=[];
const axios={{post:(url,payload)=>{{postCount+=1;calls.push({{url,payload}});return new Promise(resolve=>{{release=()=>resolve({{data:{{id:9,order_number:"TM-009"}}}});}});}}}};
const factory=new Function("axios","return async function(orderPayload) {{"+body+"}}");
const source={{customer_id:5,customer_po:"PO-1",items:[{{client_line_id:"L1",quantity:10}}]}};
const vm={{
  orderCreateSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},
  closeModal(){{closeCount+=1;}},loadOrders:async()=>true,loadKpi:async()=>true,
  showToast(){{}},showOrderNextStepGuide(){{}},errorMessage:error=>error.message,
}};
vm.saveNewOrder=factory(axios).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveNewOrder(source);
  const duplicate=await Promise.race([
    vm.saveNewOrder(source),
    new Promise((_,reject)=>setTimeout(()=>reject(new Error("duplicate stayed pending")),50)),
  ]);
  source.customer_po="CHANGED";source.items[0].quantity=99;
  expect(duplicate?._in_flight===true,"duplicate order save was not rejected");
  expect(postCount===1&&calls[0].url==="/api/orders","duplicate order POST was sent");
  expect(calls[0].payload.customer_po==="PO-1"&&calls[0].payload.items[0].quantity===10,"order payload was not frozen");
  release();const result=await first;
  expect(result.id===9&&vm.orderCreateSaveState.committed,"successful order was not committed locally");
  expect(closeCount===1,"successful order did not close original modal");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-create-duplicate.js")


def test_order_create_refresh_failure_preserves_committed_success(tmp_path: Path) -> None:
    body = _method_body("async saveNewOrder(orderPayload) {", "async saveCurrentOrderItem(orderItemId, orderItemPayload) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const axios={{post:async()=>({{data:{{id:9,order_number:"TM-009"}}}})}};
const factory=new Function("axios","return async function(orderPayload) {{"+body+"}}");
const toasts=[];let closed=false;
const vm={{
  orderCreateSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},
  closeModal(){{closed=true;}},loadOrders:async()=>{{throw new Error("orders offline");}},loadKpi:async()=>true,
  showToast:(message,isError)=>toasts.push({{message,isError}}),showOrderNextStepGuide(){{}},errorMessage:error=>error.message,
}};
(async()=>{{
  const result=await factory(axios).bind(vm)({{customer_id:5,items:[{{client_line_id:"L1"}}]}});
  if(!closed||!vm.orderCreateSaveState.committed)throw new Error("committed order was not locked and closed");
  if(!result._refresh_failed)throw new Error("order refresh failure was not reported");
  if(toasts.length!==1||!toasts[0].message.includes("订单 TM-009 已保存")||!toasts[0].message.includes("不要重复提交"))throw new Error("committed order truth was hidden");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-create-refresh.js")


def test_order_create_network_uncertain_closes_but_explicit_failure_retries(tmp_path: Path) -> None:
    body = _method_body("async saveNewOrder(orderPayload) {", "async saveCurrentOrderItem(orderItemId, orderItemPayload) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=axios=>new Function("axios","return async function(orderPayload) {{"+body+"}}")(axios);
const makeVm=()=>({{
  orderCreateSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},
  closeCount:0,closeModal(){{this.closeCount+=1;}},loadOrders:async()=>true,loadKpi:async()=>true,
  showToast(){{}},showOrderNextStepGuide(){{}},errorMessage:error=>error.message,
}});
const payload={{customer_id:5,items:[{{client_line_id:"L1"}}]}};
(async()=>{{
  const networkVm=makeVm();const networkError=new Error("network");
  try{{await factory({{post:async()=>{{throw networkError;}}}}).bind(networkVm)(payload);throw new Error("network did not throw");}}
  catch(error){{
    if(error!==networkError||!error._orderCreateOutcomeUncertain)throw error;
    if(networkVm.closeCount!==1||!networkVm.orderCreateSaveState.outcomeUncertain)throw new Error("uncertain order kept original modal active");
  }}
  const explicitVm=makeVm();const explicitError=new Error("validation");explicitError.response={{status:422}};
  try{{await factory({{post:async()=>{{throw explicitError;}}}}).bind(explicitVm)(payload);throw new Error("explicit failure did not throw");}}
  catch(error){{
    if(error!==explicitError)throw error;
    const state=explicitVm.orderCreateSaveState;
    if(explicitVm.closeCount!==0||state.saving||state.committed||state.outcomeUncertain)throw new Error("explicit failure did not leave form retryable");
  }}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-create-errors.js")


def test_save_modal_routes_new_order_outcome_without_losing_existing_errors() -> None:
    body = _method_body("async saveModal() {", "async dispatchDelivery(row, options = {}) {")
    assert 'if (this.modal?.type === "order" && (this.orderCreateSaveState.saving' in body
    assert "const createdOrder = await this.saveNewOrder(orderPayload);" in body
    assert "if (createdOrder?._in_flight) return false;" in body
    assert "if (!error?._orderCreateOutcomeUncertain)" in body
    assert "this.applyOrderRequisitionFailure(error, message);" in body
    assert "if (error?._orderCreateOutcomeUncertain)" in body
    assert "结果暂不确定" in body
    assert "按客户单号和明细核对" in body

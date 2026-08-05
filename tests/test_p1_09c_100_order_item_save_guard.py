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
    assert node is not None, "Node.js is required for order item regression"
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


def test_order_item_ui_has_dedicated_state_button_and_disabled_editor() -> None:
    assert (
        "orderItemSaveState:{saving:false,committed:false,outcomeUncertain:false,"
        "result:null}"
    ) in INDEX
    assert "orderItemSaveState.saving ? '正在保存明细…'" in INDEX
    assert "orderItemSaveState.committed ? '订单明细已保存'" in INDEX
    assert "modal?.type==='orderItem' && orderItemSaveState.saving" in INDEX
    assert '<fieldset v-else-if="modal.type === \'orderItem\'"' in INDEX
    assert ':disabled="orderItemSaveState.saving"' in INDEX
    assert "订单明细正在保存，请勿重复点击" in INDEX
    open_body = _method_body("async openOrderItem(order, item) {", "async loadFinishedInventoryCandidates(")
    assert "this.orderItemSaveState = {saving:false,committed:false,outcomeUncertain:false,result:null};" in open_body


def test_order_item_runtime_blocks_duplicate_and_freezes_payload(tmp_path: Path) -> None:
    body = _method_body("async saveCurrentOrderItem(orderItemId, orderItemPayload) {", "async saveOrderGroup(orderIds, orderPayload) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
let release,callCount=0;const calls=[];
const vm={{
  orderItemSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},orderItemForm:{{id:7,_product_error:""}},
  putIndirectProductUpdate:(url,payload,form,label)=>{{callCount+=1;calls.push({{url,payload,form,label}});return new Promise(resolve=>{{release=()=>resolve({{data:{{id:7}}}});}});}},
  closeCount:0,closeModal(){{this.closeCount+=1;}},loadOrders:async()=>true,loadProducts:async()=>true,
  showToast(){{}},errorMessage:error=>error.message,
}};
vm.saveCurrentOrderItem=new Function("return async function(orderItemId,orderItemPayload) {{"+body+"}}")().bind(vm);
const source={{quantity:30,unit_price:2.5,sync_product:true,bom_component_demands:[{{snapshot_id:1,required_piece_quantity:27}}]}};
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveCurrentOrderItem(7,source);
  const duplicate=await Promise.race([vm.saveCurrentOrderItem(7,source),new Promise((_,reject)=>setTimeout(()=>reject(new Error("duplicate pending")),50))]);
  source.quantity=99;source.bom_component_demands[0].required_piece_quantity=88;
  expect(duplicate?._in_flight===true&&callCount===1,"duplicate item save was sent");
  expect(calls[0].url==="/api/orders/items/7"&&calls[0].payload.quantity===30&&calls[0].payload.bom_component_demands[0].required_piece_quantity===27,"item payload was not frozen");
  release();const result=await first;
  expect(result.id===7&&vm.orderItemSaveState.committed,"successful item was not committed locally");
  expect(vm.closeCount===1,"successful item did not close original modal");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-item-duplicate.js")


def test_order_item_refresh_failure_preserves_committed_success(tmp_path: Path) -> None:
    body = _method_body("async saveCurrentOrderItem(orderItemId, orderItemPayload) {", "async saveOrderGroup(orderIds, orderPayload) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};const toasts=[];let closed=false;
const vm={{
  orderItemSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},orderItemForm:{{id:7}},
  putIndirectProductUpdate:async()=>({{data:{{id:7}}}}),closeModal(){{closed=true;}},
  loadOrders:async()=>{{throw new Error("orders offline");}},loadProducts:async()=>true,
  showToast:(message,isError)=>toasts.push({{message,isError}}),errorMessage:error=>error.message,
}};
(async()=>{{
  const result=await new Function("return async function(orderItemId,orderItemPayload) {{"+body+"}}")().bind(vm)(7,{{quantity:30,sync_product:false}});
  if(!closed||!vm.orderItemSaveState.committed||!result._refresh_failed)throw new Error("committed item truth was not preserved");
  if(toasts.length!==1||!toasts[0].message.includes("订单明细已保存")||!toasts[0].message.includes("不要重复提交"))throw new Error("refresh failure hid committed item");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-item-refresh.js")


def test_order_item_network_explicit_and_cancelled_results(tmp_path: Path) -> None:
    body = _method_body("async saveCurrentOrderItem(orderItemId, orderItemPayload) {", "async saveOrderGroup(orderIds, orderPayload) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};const factory=()=>new Function("return async function(orderItemId,orderItemPayload) {{"+body+"}}")();
const makeVm=()=>({{
  orderItemSaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},orderItemForm:{{id:7,_product_error:""}},closeCount:0,
  closeModal(){{this.closeCount+=1;}},loadOrders:async()=>true,loadProducts:async()=>true,showToast(){{}},errorMessage:error=>error.message,
}});
(async()=>{{
  const networkVm=makeVm();const networkError=new Error("network");networkVm.putIndirectProductUpdate=async()=>{{throw networkError;}};
  try{{await factory().bind(networkVm)(7,{{quantity:30}});throw new Error("network did not throw");}}
  catch(error){{if(error!==networkError||!error._orderItemOutcomeUncertain||networkVm.closeCount!==1||!networkVm.orderItemSaveState.outcomeUncertain)throw error;}}
  const explicitVm=makeVm();const explicit=new Error("validation");explicit.response={{status:422}};explicitVm.putIndirectProductUpdate=async()=>{{throw explicit;}};
  try{{await factory().bind(explicitVm)(7,{{quantity:30}});throw new Error("explicit did not throw");}}
  catch(error){{if(error!==explicit)throw error;const state=explicitVm.orderItemSaveState;if(explicitVm.closeCount||state.saving||state.committed||state.outcomeUncertain)throw new Error("explicit failure did not keep form retryable");}}
  const cancelledVm=makeVm();cancelledVm.putIndirectProductUpdate=async()=>null;
  const result=await factory().bind(cancelledVm)(7,{{quantity:30}});const state=cancelledVm.orderItemSaveState;
  if(result!==null||cancelledVm.closeCount||state.saving||state.committed||state.outcomeUncertain)throw new Error("confirmation cancel/version conflict was misreported");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-item-errors.js")


def test_save_modal_routes_order_item_result_without_losing_guards() -> None:
    body = _method_body("async saveModal() {", "async dispatchDelivery(row) {")
    assert 'if (this.modal?.type === "orderItem" && (this.orderItemSaveState.saving' in body
    assert "const orderItemSaved = await this.saveCurrentOrderItem(" in body
    assert "if (orderItemSaved?._in_flight || !orderItemSaved) return false;" in body
    assert "if (error?._orderItemOutcomeUncertain)" in body
    assert "结果暂不确定" in body
    assert "订单详情和常用箱" in body
    assert "payload.bom_component_demands" in body
    assert "payload.product_expected_version" in body

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
    assert node is not None, "Node.js is required for the order-item editor regression"
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


def _open_order_item_runtime(open_body: str) -> str:
    return f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];const toasts=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  modal:null,orderItemOrder:null,orderItemForm:{{}},showOrderItemDanger:false,
  finishedInventoryCandidates:[],finishedInventoryCandidatesLoaded:false,finishedInventoryReservations:[],allMaterials:[],
  beginLatestRequest(key){{global.latestRequestControllers.get(key)?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="CanceledError";}},
  showToast(message,isError){{toasts.push({{message,isError}});}},displayOrderNumber(order){{return order.order_number;}},
  money(value){{return Number(value||0).toFixed(2);}},normalizeBoxTypeDisplay(value){{return value||"";}},normalizeCuttingMode(value){{return value||"一开一";}},
  displayMaterialText(value){{return value||"";}},buildMaterialDisplay(){{return "";}},orderSpecificationText(value){{return value||"";}},
  loadFinishedInventoryReservations:async()=>true,loadExistingOrderItemBom:async()=>true
}};
vm.openOrderItem=new AsyncFunction("order","item",{json.dumps(open_body, ensure_ascii=False)}).bind(vm);
const orderA={{id:1,order_number:"A单",customer_name:"甲"}},orderB={{id:2,order_number:"B单",customer_name:"乙"}};
const itemA={{id:11,product_id:101,snapshot_product_code:"A",snapshot_product_name:"甲箱",quantity:10,unit_price:1}};
const itemB={{id:22,product_id:202,snapshot_product_code:"B",snapshot_product_name:"乙箱",quantity:20,unit_price:2}};
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
"""


def test_open_order_item_latest_product_response_wins(tmp_path: Path) -> None:
    body = _method_body("async openOrderItem(order, item) {", "async loadFinishedInventoryCandidates(")
    assert 'const requestKey = "orders:item-editor";' in body
    assert "const orderSnapshot" in body and "const itemSnapshot" in body
    assert "signal:controller.signal" in body
    assert "latestRequestControllers.get(requestKey) !== controller" in body
    script = _open_order_item_runtime(body) + """
(async()=>{
  const first=vm.openOrderItem(orderA,itemA);
  const second=vm.openOrderItem(orderB,itemB);
  pending[1].resolve({data:{id:202,box_style:"A1",version:2}});
  expect(await second===true,"latest editor did not complete");
  pending[0].resolve({data:{id:101,box_style:"A1",version:1}});
  expect(await first===false,"stale editor request was accepted");
  expect(vm.orderItemForm.id===22&&vm.orderItemOrder.id===2,"stale product response replaced the latest order item");
  expect(vm.modal?.type==="orderItem"&&toasts.length===0,"latest editor state was not retained");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "order-item-latest.js")


def test_stale_order_item_error_is_ignored(tmp_path: Path) -> None:
    body = _method_body("async openOrderItem(order, item) {", "async loadFinishedInventoryCandidates(")
    script = _open_order_item_runtime(body) + """
(async()=>{
  const first=vm.openOrderItem(orderA,itemA);
  const second=vm.openOrderItem(orderB,itemB);
  pending[1].resolve({data:{id:202,box_style:"A1"}});
  await second;
  const staleError=new Error("A读取失败");staleError.name="CanceledError";pending[0].reject(staleError);
  expect(await first===false,"stale failed editor request was accepted");
  expect(toasts.length===0&&vm.orderItemForm.id===22,"stale error leaked into the current editor");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "order-item-stale-error.js")


def test_inventory_reservations_only_update_the_expected_editor(tmp_path: Path) -> None:
    body = _method_body(
        "async loadFinishedInventoryReservations(itemId=this.orderItemForm.id, expectedForm=this.orderItemForm) {",
        "async refreshOrderItemInventorySummary(",
    )
    assert "expectedForm" in body
    assert "this.orderItemForm !== expectedForm" in body
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;const pending=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const formA={{id:11}},formB={{id:22}};
const vm={{modal:{{type:"orderItem"}},orderItemForm:formA,finishedInventoryReservations:[],showToast(){{}}}};
vm.loadFinishedInventoryReservations=new AsyncFunction("itemId","expectedForm",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadFinishedInventoryReservations(11,formA);
  vm.orderItemForm=formB;
  const second=vm.loadFinishedInventoryReservations(22,formB);
  pending[1].resolve({{data:{{items:[{{id:"B"}}]}}}});expect(await second===true,"latest reservations did not load");
  pending[0].resolve({{data:{{items:[{{id:"A"}}]}}}});expect(await first===false,"stale reservations were accepted");
  expect(vm.finishedInventoryReservations[0].id==="B","stale reservations replaced current item data");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-item-reservations.js")


def test_close_order_item_cancels_open_request_and_blocks_late_reopen(tmp_path: Path) -> None:
    open_body = _method_body("async openOrderItem(order, item) {", "async loadFinishedInventoryCandidates(")
    cancel_body = _method_body("cancelOrderItemEditorRequests() {", "async openOrderItem(order, item) {")
    close_body = _method_body("closeModal() {", "handleMasterSaveRefreshFailure(")
    assert 'if (this.modal?.type === "orderItem") this.cancelOrderItemEditorRequests();' in close_body
    script = _open_order_item_runtime(open_body) + f"""
vm.cancelOrderItemEditorRequests=new Function({json.dumps(cancel_body, ensure_ascii=False)}).bind(vm);
(async()=>{{
  const request=vm.openOrderItem(orderA,itemA);
  vm.cancelOrderItemEditorRequests();vm.modal=null;
  pending[0].resolve({{data:{{id:101,box_style:"A1"}}}});
  expect(await request===false,"closed editor accepted a late response");
  expect(vm.modal===null&&!vm.orderItemForm.id,"late response reopened a closed editor");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-item-close.js")


def test_open_order_item_preserves_existing_read_only_load_contracts() -> None:
    body = _method_body("async openOrderItem(order, item) {", "async loadFinishedInventoryCandidates(")
    assert "/api/master/products/${itemSnapshot.product_id}" in body
    assert "loadFinishedInventoryReservations(itemSnapshot.id, activeForm)" in body
    assert "loadExistingOrderItemBom(activeForm)" in body
    assert "axios.post" not in body
    assert "axios.patch" not in body
    assert "axios.delete" not in body

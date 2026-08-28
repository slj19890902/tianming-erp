from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX, f"missing method: {signature}"
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(tmp_path: Path, name: str, script: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for receipt action regression"
    target = tmp_path / name
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_receipt_buttons_and_modal_expose_visible_busy_states() -> None:
    delivery = INDEX[
        INDEX.index('<template v-else-if="activePage === \'deliveries\'">') :
        INDEX.index('<template v-else-if="activePage === \'finance\'">')
    ]

    assert "receiptOperationState" in INDEX
    assert "receiptOperationState.action==='open'" in delivery
    assert "receiptOperationState.action==='cancel'" in delivery
    assert "加载中…" in delivery
    assert "取消中…" in delivery
    assert "modal?.type==='receipt'" in INDEX
    assert "保存中…" in INDEX
    assert "保存回单" in INDEX


def test_open_save_and_cancel_receipt_are_single_flight_and_freeze_targets(
    tmp_path: Path,
) -> None:
    open_body = _method_body("async openReceipt(row) {", "async saveReceipt() {")
    save_body = _method_body(
        "async saveReceipt() {",
        "resetDeliveryReminderState({preserveAcknowledgement=false} = {}) {",
    )
    cancel_body = _method_body("async cancelReceipt(row) {", "exportStatement(row) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
const messages = [];
let confirms = [];
globalThis.confirm = () => confirms.shift() ?? true;
globalThis.today = () => "2026-08-05";
globalThis.createIdempotencyKey = () => "receipt-test-key";
globalThis.axios = {{
  get(url) {{ return new Promise((resolve, reject) => pending.push({{method:"get",url,resolve,reject}})); }},
  post(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"post",url,payload,resolve,reject}})); }},
  put(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"put",url,payload,resolve,reject}})); }},
}};
const vm = {{
  modal:null,
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  receiptOperationState:{{action:"",deliveryId:null,receiptId:null,deliveryNumber:""}},
  receiptReminderState:{{bundleAttempt:null}},
  receiptMonthOptions:{{previous:"2026-07",current:"2026-08",next:"2026-09"}},
  receiptForm:{{id:null,delivery_id:21,actual_received_date:"2026-08-05",reconciliation_month:"2026-08",signed_by:"客户签收",items:[{{delivery_item_id:1,product_code:"P1",product_name:"外箱",delivered_quantity:10,actual_received_quantity:10,resolution_action:"",difference_reason:"",return_location_id:null}}]}},
  deliveries:[],
  productionLocations:[],
  validateReceiptForm() {{ return ""; }},
  async ensureProductionLocations() {{ return true; }},
  resetReceiptReminderModalState() {{ this.receiptReminderState = {{bundleAttempt:null}}; }},
  async loadReceiptReminderProducts() {{ return true; }},
  async loadReceiptReminders() {{ return true; }},
  productionLocationFloorNumber() {{ return null; }},
  productionLocationAreaCode() {{ return ""; }},
  async loadDeliveries() {{ return true; }},
  async loadOrders() {{ return true; }},
  async loadKpi() {{ return true; }},
  async loadFinance() {{ return true; }},
  invalidateDeliveryListDetail() {{ return true; }},
  closeModal() {{ this.modal = null; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.openReceipt = new AsyncFunction("row", {json.dumps(open_body, ensure_ascii=False)}).bind(vm);
vm.saveReceipt = new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
vm.cancelReceipt = new AsyncFunction("row", {json.dumps(cancel_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const openRow = {{id:11,delivery_number:"TH011",return_receipt_id:null}};
  const opening = vm.openReceipt(openRow);
  const duplicateOpen = vm.openReceipt({{id:12,delivery_number:"TH012",return_receipt_id:null}});
  await Promise.resolve();
  openRow.id = 99;
  if (pending.length !== 2 || pending[0].url !== "/api/deliveries/11" || pending[1].url !== "/api/finance/reconciliation-month-options") throw new Error("receipt open duplicated or target changed");
  if (await duplicateOpen !== false) throw new Error("duplicate receipt open was not rejected");
  pending[0].resolve({{data:{{id:11,items:[{{id:101,product_code:"P1",product_name:"外箱",delivered_quantity:10,source_type:"order",requires_return_location:false}}]}}}});
  pending[1].resolve({{data:{{previous:"2026-07",current:"2026-08",next:"2026-09"}}}});
  if (await opening !== true || vm.receiptOperationState.action || vm.receiptForm.delivery_id !== 11 || vm.modal?.type !== "receipt") throw new Error("receipt open did not complete and unlock");

  vm.receiptForm = {{id:null,delivery_id:21,actual_received_date:"2026-08-05",reconciliation_month:"2026-08",signed_by:"客户签收",idempotency_key:null,_mutation_signature:"",items:[{{delivery_item_id:101,product_code:"P1",product_name:"外箱",delivered_quantity:10,actual_received_quantity:10,resolution_action:"",difference_reason:"",return_location_id:null}}]}};
  vm.modal = {{type:"receipt"}};
  const saving = vm.saveReceipt();
  const duplicateSave = vm.saveReceipt();
  await Promise.resolve();
  vm.receiptForm.delivery_id = 98;
  if (pending.length !== 3 || pending[2].url !== "/api/finance/return_receipts" || pending[2].payload.delivery_id !== 21) throw new Error("receipt save duplicated or target changed");
  if (await duplicateSave !== false) throw new Error("duplicate receipt save was not rejected");
  pending[2].resolve({{data:{{id:301,status:"confirmed"}}}});
  if (await saving !== true || vm.receiptOperationState.action || vm.modal !== null) throw new Error("receipt save did not complete and unlock");

  confirms = [true];
  vm.modal = null;
  const cancelRow = {{id:31,delivery_number:"TH031",return_receipt_id:401}};
  const cancelling = vm.cancelReceipt(cancelRow);
  const duplicateCancel = vm.cancelReceipt({{id:32,delivery_number:"TH032",return_receipt_id:402}});
  await Promise.resolve();
  cancelRow.return_receipt_id = 999;
  if (pending.length !== 4 || pending[3].url !== "/api/finance/return_receipts/401/cancel") throw new Error("receipt cancel duplicated or target changed");
  if (await duplicateCancel !== false) throw new Error("duplicate receipt cancel was not rejected");
  pending[3].resolve({{data:{{status:"cancelled"}}}});
  if (await cancelling !== true || vm.receiptOperationState.action) throw new Error("receipt cancel did not complete and unlock");

  confirms = [false];
  const confirmationCancelled = await vm.cancelReceipt({{id:41,delivery_number:"TH041",return_receipt_id:501}});
  if (confirmationCancelled !== false || vm.receiptOperationState.action || pending.length !== 4) throw new Error("cancelled confirmation had side effects");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "return-receipt-single-flight.js", script)


def test_receipt_success_is_not_misreported_when_refresh_fails(tmp_path: Path) -> None:
    save_body = _method_body(
        "async saveReceipt() {",
        "resetDeliveryReminderState({preserveAcknowledgement=false} = {}) {",
    )
    cancel_body = _method_body("async cancelReceipt(row) {", "exportStatement(row) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
let mode = "refresh-fails";
const messages = [];
const invalidated = [];
globalThis.confirm = () => true;
globalThis.createIdempotencyKey = () => "receipt-test-key";
globalThis.axios = {{
  async post(url) {{ if (mode === "save-fails" && !url.endsWith("/cancel")) throw new Error("回单状态变化"); if (mode === "cancel-fails" && url.endsWith("/cancel")) throw new Error("已进入对账"); return {{data:{{id:301}}}}; }},
  async put() {{ if (mode === "save-fails") throw new Error("回单状态变化"); return {{data:{{id:301}}}}; }},
}};
const vm = {{
  modal:{{type:"receipt"}},
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  receiptOperationState:{{action:"",deliveryId:null,receiptId:null,deliveryNumber:""}},
  receiptReminderState:{{bundleAttempt:null}},
  receiptForm:{{id:null,delivery_id:21,actual_received_date:"2026-08-05",reconciliation_month:"2026-08",signed_by:"客户签收",idempotency_key:null,_mutation_signature:"",items:[{{delivery_item_id:1,delivered_quantity:10,actual_received_quantity:10,resolution_action:"",difference_reason:"",return_location_id:null}}]}},
  deliveries:[],
  validateReceiptForm() {{ return ""; }},
  async loadDeliveries() {{ if (mode === "refresh-fails") throw new Error("刷新断开"); return true; }},
  async loadOrders() {{ return true; }},
  async loadKpi() {{ return true; }},
  async loadFinance() {{ return true; }},
  invalidateDeliveryListDetail(id) {{ invalidated.push(Number(id)); return true; }},
  closeModal() {{ this.modal = null; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.saveReceipt = new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
vm.cancelReceipt = new AsyncFunction("row", {json.dumps(cancel_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const saved = await vm.saveReceipt();
  if (saved !== true || vm.receiptOperationState.action || vm.modal !== null) throw new Error("successful receipt save was misreported");
  if (!invalidated.includes(21)) throw new Error("successful receipt save kept stale detail");
  if (!messages.some(row => row.danger && row.message.includes("回单已经保存") && row.message.includes("刷新") && row.message.includes("不要重复"))) throw new Error("receipt save refresh failure lacked anti-repeat guidance");

  messages.length = 0; vm.modal = null;
  const cancelled = await vm.cancelReceipt({{id:22,delivery_number:"TH022",return_receipt_id:302}});
  if (cancelled !== true || vm.receiptOperationState.action) throw new Error("successful receipt cancel was misreported");
  if (!invalidated.includes(22)) throw new Error("successful receipt cancel kept stale detail");
  if (!messages.some(row => row.danger && row.message.includes("回单已经取消") && row.message.includes("刷新") && row.message.includes("不要重复"))) throw new Error("receipt cancel refresh failure lacked anti-repeat guidance");

  mode = "save-fails"; messages.length = 0; vm.modal = {{type:"receipt"}};
  const saveFailed = await vm.saveReceipt();
  if (saveFailed !== false || vm.receiptOperationState.action || vm.modal === null || !messages.some(row => row.danger && row.message.includes("回单状态变化"))) throw new Error("receipt save failure lost real error or modal");

  mode = "cancel-fails"; messages.length = 0;
  const cancelFailed = await vm.cancelReceipt({{id:23,delivery_number:"TH023",return_receipt_id:303}});
  if (cancelFailed !== false || vm.receiptOperationState.action || !messages.some(row => row.danger && row.message.includes("已进入对账"))) throw new Error("receipt cancel failure lost real error or lock");
  if (invalidated.includes(23)) throw new Error("failed receipt cancel invalidated detail");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "return-receipt-followup.js", script)


def test_return_receipt_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "return-receipt-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

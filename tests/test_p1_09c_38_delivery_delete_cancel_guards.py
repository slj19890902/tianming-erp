from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(tmp_path: Path, name: str, script: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for delivery action regression"
    target = tmp_path / name
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_delete_and_cancel_buttons_share_delivery_busy_state() -> None:
    delivery = INDEX[
        INDEX.index('<template v-else-if="activePage === \'deliveries\'">') :
        INDEX.index('<template v-else-if="activePage === \'finance\'">')
    ]

    assert "deliveryOperationState.action==='delete'" in delivery
    assert "deliveryOperationState.action==='cancel'" in delivery
    assert "删除中…" in delivery
    assert "取消中…" in delivery
    # Inline editing adds another guard; keep checking both original busy gates
    # without requiring that they are the only guards on the button.
    assert delivery.count(":disabled=\"!!deliveryOperationState.action || !!receiptOperationState.action") >= 8


def test_delete_and_cancel_are_single_flight_and_freeze_target(tmp_path: Path) -> None:
    delete_body = _method_body("async deleteDelivery(row) {", "async cancelDelivery(row) {")
    cancel_body = _method_body("async cancelDelivery(row) {", "async cancelReceipt(row) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
const messages = [];
let confirms = [];
globalThis.confirm = () => confirms.shift() ?? true;
globalThis.axios = {{
  delete(url) {{ return new Promise((resolve, reject) => pending.push({{method:"delete",url,resolve,reject}})); }},
  put(url) {{ return new Promise((resolve, reject) => pending.push({{method:"put",url,resolve,reject}})); }},
}};
const vm = {{
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  async loadDeliveries() {{ return true; }},
  async loadOrders() {{ return true; }},
  async loadKpi() {{ return true; }},
  invalidateDeliveryListDetail() {{ return true; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.deleteDelivery = new AsyncFunction("row", {json.dumps(delete_body, ensure_ascii=False)}).bind(vm);
vm.cancelDelivery = new AsyncFunction("row", {json.dumps(cancel_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  confirms = [true];
  const deleteRow = {{id:11,delivery_number:"TH011"}};
  const deleting = vm.deleteDelivery(deleteRow);
  const duplicateDelete = vm.deleteDelivery({{id:12,delivery_number:"TH012"}});
  await Promise.resolve();
  deleteRow.id = 99;
  if (pending.length !== 1 || pending[0].url !== "/api/deliveries/11") throw new Error("delete request duplicated or target changed");
  if (await duplicateDelete !== false) throw new Error("duplicate delete was not rejected");
  pending[0].resolve({{data:{{deleted:true,voided:false}}}});
  if (await deleting !== true || vm.deliveryOperationState.action) throw new Error("delete did not complete and unlock");

  confirms = [true];
  const cancelRow = {{id:21,delivery_number:"TH021"}};
  const cancelling = vm.cancelDelivery(cancelRow);
  const duplicateCancel = vm.cancelDelivery({{id:22,delivery_number:"TH022"}});
  await Promise.resolve();
  cancelRow.id = 98;
  if (pending.length !== 2 || pending[1].url !== "/api/deliveries/21/cancel") throw new Error("cancel request duplicated or target changed");
  if (await duplicateCancel !== false) throw new Error("duplicate cancel was not rejected");
  pending[1].resolve({{data:{{ok:true}}}});
  if (await cancelling !== true || vm.deliveryOperationState.action) throw new Error("cancel did not complete and unlock");

  confirms = [false];
  const confirmationCancelled = await vm.cancelDelivery({{id:31,delivery_number:"TH031"}});
  if (confirmationCancelled !== false || vm.deliveryOperationState.action || pending.length !== 2) throw new Error("cancelled confirmation had side effects");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "delivery-delete-cancel-single-flight.js", script)


def test_successful_delete_or_cancel_is_not_misreported_when_refresh_fails(
    tmp_path: Path,
) -> None:
    delete_body = _method_body("async deleteDelivery(row) {", "async cancelDelivery(row) {")
    cancel_body = _method_body("async cancelDelivery(row) {", "async cancelReceipt(row) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
let mode = "refresh-fails";
const messages = [];
const invalidated = [];
globalThis.confirm = () => true;
globalThis.axios = {{
  async delete() {{ if (mode === "delete-fails") throw new Error("删除冲突"); return {{data:{{voided:false}}}}; }},
  async put() {{ if (mode === "cancel-fails") throw new Error("已有回单"); return {{data:{{ok:true}}}}; }},
}};
const vm = {{
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  async loadDeliveries() {{ if (mode === "refresh-fails") throw new Error("刷新断开"); return true; }},
  async loadOrders() {{ return true; }},
  async loadKpi() {{ return true; }},
  invalidateDeliveryListDetail(id) {{ invalidated.push(Number(id)); return true; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.deleteDelivery = new AsyncFunction("row", {json.dumps(delete_body, ensure_ascii=False)}).bind(vm);
vm.cancelDelivery = new AsyncFunction("row", {json.dumps(cancel_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const deleted = await vm.deleteDelivery({{id:41,delivery_number:"TH041"}});
  if (deleted !== true || vm.deliveryOperationState.action) throw new Error("successful delete was misreported");
  if (!invalidated.includes(41)) throw new Error("successful delete kept stale detail");
  if (!messages.some(row => row.danger && row.message.includes("已经删除") && row.message.includes("刷新") && row.message.includes("不要重复"))) throw new Error("delete refresh failure lacked anti-repeat guidance");

  messages.length = 0;
  const cancelled = await vm.cancelDelivery({{id:42,delivery_number:"TH042"}});
  if (cancelled !== true || vm.deliveryOperationState.action) throw new Error("successful cancel was misreported");
  if (!invalidated.includes(42)) throw new Error("successful cancel kept stale detail");
  if (!messages.some(row => row.danger && row.message.includes("取消发货已经完成") && row.message.includes("刷新") && row.message.includes("不要重复"))) throw new Error("cancel refresh failure lacked anti-repeat guidance");

  mode = "delete-fails"; messages.length = 0;
  const deleteFailed = await vm.deleteDelivery({{id:43,delivery_number:"TH043"}});
  if (deleteFailed !== false || vm.deliveryOperationState.action || !messages.some(row => row.danger && row.message.includes("删除冲突"))) throw new Error("delete failure lost real error or lock");
  if (invalidated.includes(43)) throw new Error("failed delete invalidated detail");

  mode = "cancel-fails"; messages.length = 0;
  const cancelFailed = await vm.cancelDelivery({{id:44,delivery_number:"TH044"}});
  if (cancelFailed !== false || vm.deliveryOperationState.action || !messages.some(row => row.danger && row.message.includes("已有回单"))) throw new Error("cancel failure lost real error or lock");
  if (invalidated.includes(44)) throw new Error("failed cancel invalidated detail");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "delivery-delete-cancel-followup.js", script)


def test_delivery_delete_cancel_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "delivery-delete-cancel-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

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


def test_delivery_pick_and_dispatch_buttons_share_a_visible_busy_state() -> None:
    delivery = INDEX[
        INDEX.index('<template v-else-if="activePage === \'deliveries\'">') :
        INDEX.index('<template v-else-if="activePage === \'finance\'">')
    ]

    assert "deliveryOperationState.action==='pick'" in delivery
    assert "deliveryOperationState.action==='dispatch'" in delivery
    assert "推送中…" in delivery
    assert "发货中…" in delivery
    assert "!!deliveryOperationState.action" in delivery
    footer_pick = INDEX[INDEX.index('modal?.type === \'delivery\' && deliveryForm.editingId') : INDEX.index('<button v-if="canSaveModal"')]
    assert "createCurrentDeliveryPickTask" in footer_pick
    assert "deliveryOperationState" in footer_pick


def test_pick_and_dispatch_are_single_flight_and_freeze_the_delivery(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for delivery action regression"
    pick_body = _method_body("async createDeliveryPickTask(row) {", "openDeliveryPickTask(row) {")
    dispatch_body = _method_body("async dispatchDelivery(row) {", "async printDelivery(row) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
const messages = [];
let confirms = [];
globalThis.confirm = () => confirms.shift() ?? true;
globalThis.axios = {{
  post(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"post",url,payload,resolve,reject}})); }},
  put(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"put",url,payload,resolve,reject}})); }},
}};
const printSessions = [];
const vm = {{
  loading:false,
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  normalizeDeliveryPickTask(row) {{ return row?.pick_task || null; }},
  deliveryPickExceptionText() {{ return ""; }},
  async applyDeliveryPickTask() {{ throw new Error("unexpected pick apply"); }},
  async openDeliveryPrintTab(id, defer) {{
    const session = {{id,defer,activated:0,aborted:0,activate(){{this.activated+=1;}},abort(){{this.aborted+=1;}}}};
    printSessions.push(session);
    return session;
  }},
  async loadDeliveries() {{ return true; }},
  async loadOrders() {{ return true; }},
  async loadKpi() {{ return true; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.createDeliveryPickTask = new AsyncFunction("row", {json.dumps(pick_body, ensure_ascii=False)}).bind(vm);
vm.dispatchDelivery = new AsyncFunction("row", {json.dumps(dispatch_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const pickRow = {{id:11,delivery_number:"TH001",pick_task:null}};
  const picking = vm.createDeliveryPickTask(pickRow);
  const duplicatePick = vm.createDeliveryPickTask({{id:12,delivery_number:"TH002",pick_task:null}});
  await Promise.resolve();
  pickRow.id = 99;
  if (pending.length !== 1 || pending[0].url !== "/api/deliveries/11/pick-task") throw new Error("pick request duplicated or target was not frozen");
  if (await duplicatePick !== null) throw new Error("duplicate pick was not rejected");
  pending[0].resolve({{data:{{pick_task:{{id:301,status:"created",assignment_required:false,assigned_to_name:"送货员"}}}}}});
  const picked = await picking;
  if (!picked || vm.deliveryOperationState.action || pickRow.pick_task?.id !== 301) throw new Error("pick did not update and unlock");

  confirms = [true];
  const deliveryRow = {{id:21,delivery_number:"TH003",status:"pending",pick_task:null}};
  const dispatching = vm.dispatchDelivery(deliveryRow);
  const duplicateDispatch = vm.dispatchDelivery({{id:22,delivery_number:"TH004",status:"pending",pick_task:null}});
  await Promise.resolve();
  deliveryRow.id = 98;
  if (printSessions.length !== 1 || printSessions[0].id !== 21) throw new Error("dispatch print session duplicated or target was not frozen");
  if (await duplicateDispatch !== false) throw new Error("duplicate dispatch was not rejected");
  if (pending.length !== 2 || pending[1].url !== "/api/deliveries/21/dispatch") throw new Error("dispatch request missing or target changed");
  pending[1].resolve({{data:{{ok:true}}}});
  await Promise.resolve();
  if (pending.length !== 3 || pending[2].url !== "/api/deliveries/21/printed") throw new Error("printed marker missing or target changed");
  pending[2].resolve({{data:{{ok:true}}}});
  if (await dispatching !== true || vm.deliveryOperationState.action || printSessions[0].activated !== 1 || printSessions[0].aborted) throw new Error("dispatch did not activate print and unlock");

  confirms = [false];
  const cancelled = await vm.dispatchDelivery({{id:30,delivery_number:"TH005",pick_task:null}});
  if (cancelled !== false || vm.deliveryOperationState.action || pending.length !== 3 || printSessions.length !== 1) throw new Error("cancelled confirmation had side effects");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "delivery-action-single-flight.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_dispatch_success_is_not_reported_as_failure_when_followup_breaks(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    dispatch_body = _method_body("async dispatchDelivery(row) {", "async printDelivery(row) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
let mode = "printed-fails";
let putCalls = [];
const messages = [];
globalThis.confirm = () => true;
globalThis.axios = {{
  async put(url) {{
    putCalls.push(url);
    if (url.endsWith("/printed") && mode === "printed-fails") throw new Error("打印登记断开");
    if (url.endsWith("/dispatch") && mode === "dispatch-fails") throw new Error("库存版本变化");
    return {{data:{{ok:true}}}};
  }}
}};
const sessions = [];
const vm = {{
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  normalizeDeliveryPickTask() {{ return null; }},
  deliveryPickExceptionText() {{ return ""; }},
  async openDeliveryPrintTab(id) {{ const row={{id,activated:0,aborted:0,activate(){{this.activated+=1;}},abort(){{this.aborted+=1;}}}}; sessions.push(row); return row; }},
  async loadDeliveries() {{ if (mode === "refresh-fails") throw new Error("刷新断开"); }},
  async loadOrders() {{ return true; }},
  async loadKpi() {{ return true; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.dispatchDelivery = new AsyncFunction("row", {json.dumps(dispatch_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const printedFailed = await vm.dispatchDelivery({{id:41,delivery_number:"TH041"}});
  if (printedFailed !== true || vm.deliveryOperationState.action || sessions[0].activated !== 1 || sessions[0].aborted) throw new Error("successful dispatch was misreported after printed failure");
  if (!messages.some(row => row.danger && row.message.includes("发货已经完成") && row.message.includes("不要再次发货"))) throw new Error("printed failure lacked anti-repeat guidance");

  mode = "refresh-fails"; putCalls = []; messages.length = 0;
  const refreshFailed = await vm.dispatchDelivery({{id:42,delivery_number:"TH042"}});
  if (refreshFailed !== true || vm.deliveryOperationState.action || sessions[1].activated !== 1 || sessions[1].aborted) throw new Error("successful dispatch was misreported after refresh failure");
  if (!messages.some(row => row.danger && row.message.includes("发货已经完成") && row.message.includes("刷新") && row.message.includes("不要再次发货"))) throw new Error("refresh failure lacked anti-repeat guidance");

  mode = "dispatch-fails"; putCalls = []; messages.length = 0;
  const dispatchFailed = await vm.dispatchDelivery({{id:43,delivery_number:"TH043"}});
  if (dispatchFailed !== false || vm.deliveryOperationState.action || sessions[2].aborted !== 1 || sessions[2].activated) throw new Error("failed dispatch did not abort print and unlock");
  if (!messages.some(row => row.danger && row.message.includes("库存版本变化"))) throw new Error("dispatch failure lost the real error");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "delivery-action-followup.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_delivery_action_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "delivery-action-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

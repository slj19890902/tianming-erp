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
    assert node is not None, "Node.js is required for delivery picker regression"
    target = tmp_path / name
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_picker_assignment_exposes_visible_busy_state() -> None:
    delivery = INDEX[
        INDEX.index('<template v-else-if="activePage === \'deliveries\'">') :
        INDEX.index('<template v-else-if="activePage === \'finance\'">')
    ]

    assert "deliveryOperationState.action==='assign'" in delivery
    assert "分配中…" in delivery
    assert "deliveryOperationState.taskId" in delivery
    assert "!!deliveryOperationState.action || !!receiptOperationState.action" in delivery


def test_picker_assignment_is_single_flight_and_freezes_task_and_picker(
    tmp_path: Path,
) -> None:
    body = _method_body(
        "async assignDeliveryPickTask(row, pickerUserId) {",
        "async applyDeliveryPickTask(row) {",
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
const messages = [];
globalThis.axios = {{
  put(url, payload) {{ return new Promise((resolve, reject) => pending.push({{url,payload,resolve,reject}})); }},
}};
const vm = {{
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  receiptOperationState:{{action:"",deliveryId:null,receiptId:null,deliveryNumber:""}},
  deliveryListState:{{error:""}},
  normalizeDeliveryPickTask(row) {{ return row?.pick_task || null; }},
  async loadDeliveries() {{ return true; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.assignDeliveryPickTask = new AsyncFunction("row", "pickerUserId", {json.dumps(body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const staleRow = {{id:11,delivery_number:"TH011",pick_task:{{id:101,assigned_to:3}}}};
  const assigning = vm.assignDeliveryPickTask(staleRow, "7");
  const duplicate = vm.assignDeliveryPickTask({{id:12,delivery_number:"TH012",pick_task:{{id:102}}}}, "8");
  await Promise.resolve();
  staleRow.id = 99;
  staleRow.pick_task = {{id:999,assigned_to:9}};
  if (pending.length !== 1 || pending[0].url !== "/api/delivery-picks/101/assignment" || pending[0].payload.picker_user_id !== 7) throw new Error("assignment duplicated or frozen target changed");
  if (await duplicate !== false) throw new Error("duplicate assignment was not rejected");
  pending[0].resolve({{data:{{id:101,assigned_to:7,assigned_to_name:"送货甲"}}}});
  if (await assigning !== true || vm.deliveryOperationState.action || staleRow.pick_task.id !== 999) throw new Error("stale row was overwritten or assignment did not unlock");

  const currentRow = {{id:21,delivery_number:"TH021",pick_task:{{id:201,assigned_to:7}}}};
  const unassigning = vm.assignDeliveryPickTask(currentRow, "");
  await Promise.resolve();
  if (pending.length !== 2 || pending[1].payload.picker_user_id !== null) throw new Error("unassignment payload was not frozen as null");
  pending[1].resolve({{data:{{id:201,assigned_to:null,assigned_to_name:null,assignment_required:true}}}});
  if (await unassigning !== true || currentRow.pick_task.assigned_to !== null || vm.deliveryOperationState.action) throw new Error("current row was not updated after unassignment");
  if (!messages.some(row => !row.danger && row.message.includes("已取消分配"))) throw new Error("unassignment success message missing");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "delivery-picker-assignment-single-flight.js", script)


def test_picker_assignment_reports_request_and_refresh_failures_without_escape(
    tmp_path: Path,
) -> None:
    body = _method_body(
        "async assignDeliveryPickTask(row, pickerUserId) {",
        "async applyDeliveryPickTask(row) {",
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const messages = [];
let refreshMode = "state-error";
globalThis.axios = {{ async put() {{ throw new Error("拿货已开始，不能改派送货员"); }} }};
const vm = {{
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  receiptOperationState:{{action:"",deliveryId:null,receiptId:null,deliveryNumber:""}},
  deliveryListState:{{error:""}},
  normalizeDeliveryPickTask(row) {{ return row?.pick_task || null; }},
  async loadDeliveries() {{
    if (refreshMode === "throws") throw new Error("列表连接失败");
    this.deliveryListState.error = "列表刷新失败";
  }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.assignDeliveryPickTask = new AsyncFunction("row", "pickerUserId", {json.dumps(body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const stateError = await vm.assignDeliveryPickTask({{id:31,delivery_number:"TH031",pick_task:{{id:301}}}}, "7");
  if (stateError !== false || vm.deliveryOperationState.action) throw new Error("assignment failure did not unlock");
  if (!messages.some(row => row.danger && row.message.includes("拿货已开始") && row.message.includes("列表刷新失败"))) throw new Error("state refresh failure was not combined");

  refreshMode = "throws"; messages.length = 0; vm.deliveryListState.error = "";
  const thrownRefresh = await vm.assignDeliveryPickTask({{id:32,delivery_number:"TH032",pick_task:{{id:302}}}}, "8");
  if (thrownRefresh !== false || vm.deliveryOperationState.action) throw new Error("thrown refresh failure escaped or lock remained");
  if (!messages.some(row => row.danger && row.message.includes("拿货已开始") && row.message.includes("列表连接失败"))) throw new Error("thrown refresh failure was not combined");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "delivery-picker-assignment-failure.js", script)


def test_delivery_picker_assignment_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "delivery-picker-assignment-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

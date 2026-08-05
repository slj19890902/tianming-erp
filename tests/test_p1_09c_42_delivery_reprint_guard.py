from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(start: str, end: str) -> str:
    start_index = INDEX.index(start) + len(start)
    body = INDEX[start_index : INDEX.index(end, start_index)]
    return re.sub(r"\n\s*},\s*$", "", body)


def _run_node(tmp_path: Path, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for delivery reprint regression"
    target = tmp_path / "p1-09c-42-delivery-reprint.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_reprint_button_shows_busy_state_and_shares_delivery_lock() -> None:
    delivery = INDEX[
        INDEX.index('<template v-else-if="activePage === \'deliveries\'">') :
        INDEX.index('<template v-else-if="activePage === \'finance\'">')
    ]
    assert "deliveryOperationState.action==='reprint'" in delivery
    assert "打开中…" in delivery
    assert '@click="printDelivery(row)"' in delivery
    assert ':disabled="!!deliveryOperationState.action || !!receiptOperationState.action"' in delivery


def test_reprint_is_single_flight_freezes_target_and_recovers(tmp_path: Path) -> None:
    body = _method_body("async printDelivery(row) {", "async editDelivery(row) {")
    assert "/dispatch" not in body
    assert "/printed" not in body
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const opened = [];
const messages = [];
let mode = "pending";
let resolveOpen;
const vm = {{
  deliveryOperationState:{{action:"",deliveryId:null,deliveryNumber:""}},
  receiptOperationState:{{action:"",deliveryId:null}},
  openDeliveryPrintTab(id) {{
    opened.push(id);
    if (mode === "pending") return new Promise(resolve => {{ resolveOpen = resolve; }});
    if (mode === "blocked") return Promise.resolve(null);
    if (mode === "throws") return Promise.reject(new Error("打印通道异常"));
    return Promise.resolve(true);
  }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }},
}};
vm.printDelivery = new AsyncFunction("row", {json.dumps(body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const row = {{id:15,delivery_number:"TH015"}};
  const first = vm.printDelivery(row);
  const duplicate = vm.printDelivery({{id:16,delivery_number:"TH016"}});
  row.id = 99;
  if (opened.length !== 1 || opened[0] !== 15) throw new Error("reprint duplicated or target was not frozen");
  if (await duplicate !== false) throw new Error("duplicate reprint was not rejected");
  if (vm.deliveryOperationState.action !== "reprint" || vm.deliveryOperationState.deliveryId !== 15) throw new Error("busy state did not freeze delivery");
  resolveOpen(true);
  if (await first !== true || vm.deliveryOperationState.action) throw new Error("successful reprint did not unlock");

  mode = "blocked";
  if (await vm.printDelivery({{id:20,delivery_number:"TH020"}}) !== false || vm.deliveryOperationState.action) throw new Error("blocked print did not return false and unlock");

  mode = "throws";
  if (await vm.printDelivery({{id:21,delivery_number:"TH021"}}) !== false || vm.deliveryOperationState.action) throw new Error("failed print did not return false and unlock");
  if (!messages.some(row => row.danger && row.message.includes("TH021") && row.message.includes("打印通道异常"))) throw new Error("real print error was not shown");

  const beforeInvalid = opened.length;
  if (await vm.printDelivery({{id:null,delivery_number:""}}) !== false || opened.length !== beforeInvalid) throw new Error("invalid delivery tried to print");
  if (!messages.some(row => row.danger && row.message.includes("送货单不存在"))) throw new Error("invalid delivery lacked feedback");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, source)


def test_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-42-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

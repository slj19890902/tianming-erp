from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.start() : match.end() + next_method.start()]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend behavior validation"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_incoming_tabs_expose_independent_loading_error_and_retry_states() -> None:
    for marker in (
        "incomingPendingState",
        "incomingHistoryState",
        "正在读取待入库",
        "待入库读取失败",
        "正在读取历史入库",
        "历史入库读取失败",
        'loadIncoming({force:true})',
        'loadIncomingHistory({force:true})',
    ):
        assert marker in INDEX

    pending = _method_body("loadIncoming")
    history = _method_body("loadIncomingHistory")
    assert 'latestRequestControllers.get("incoming:pending") !== controller' in pending
    assert 'latestRequestControllers.get("incoming:history") !== controller' in history
    assert "this.incomingPending = []" not in pending
    assert "this.incomingHistory = []" not in history


def test_incoming_formal_actions_are_single_flight_and_show_busy_labels() -> None:
    for marker in (
        "incomingOperationState",
        "incomingReceiveAttempts",
        "incomingBatchAttempt",
        "incomingOperationBusy()",
        "批量入库中…",
        "入库中…",
        "结单中…",
        "撤销中…",
    ):
        assert marker in INDEX

    for name in (
        "batchReceiveIncoming",
        "receiveIncoming",
        "acceptShortIncoming",
        "revertIncoming",
    ):
        body = _method_body(name)
        assert "incomingOperationBusy()" in body
        assert "incomingOperationState" in body
        assert "finally" in body


def test_receive_is_single_flight_and_refresh_failure_does_not_report_write_failure(
    tmp_path: Path,
) -> None:
    method_names = (
        "incomingProjectedVariance",
        "incomingPayload",
        "incomingOperationKey",
        "incomingOperationBusy",
        "incomingOperationIs",
        "incomingReceiveAttempt",
        "receiveIncoming",
    )
    methods = ",\n".join(_method_body(name).strip().rstrip(",") for name in method_names)
    script = f"""
const methods = {{
{methods}
}};
let resolveWrite;
let putCalls = 0;
globalThis.confirm = () => true;
globalThis.createIdempotencyKey = () => "uat-incoming-key";
globalThis.axios = {{
  put() {{
    putCalls += 1;
    return new Promise(resolve => {{ resolveWrite = resolve; }});
  }}
}};
const notices = [];
const row = {{
  item_id: 9,
  incoming_quantity: 5,
  cumulative_received_quantity: 0,
  planned_quantity: 5,
  resolution_action: "",
  resolution_reason: "",
  surplus_location_id: null,
}};
const context = {{
  incomingOperationState: {{action:"", key:""}},
  incomingReceiveAttempts: {{}},
  incomingLocationsLoading: false,
  incomingLocationsError: "",
  refreshIncomingAfterWrite: async () => {{ throw new Error("刷新连接失败"); }},
  errorMessage: error => String(error?.message || error),
  showToast(message, danger) {{ notices.push({{message, danger:!!danger}}); }},
}};
for (const [name, method] of Object.entries(methods)) context[name] = method;
(async () => {{
  const first = methods.receiveIncoming.call(context, row);
  const second = methods.receiveIncoming.call(context, row);
  if (putCalls !== 1) throw new Error(`duplicate receive request: ${{putCalls}}`);
  resolveWrite({{data:{{material_status:"received",remaining_quantity:0}}}});
  const [firstResult, secondResult] = await Promise.all([first, second]);
  if (firstResult !== true || secondResult !== false) throw new Error("single-flight results are wrong");
  if (context.incomingOperationState.action) throw new Error("operation lock was not released");
  const success = notices.find(row => row.message.includes("入库已成功") && row.message.includes("刷新失败"));
  if (!success || !success.danger) throw new Error("successful write refresh warning is missing");
  if (Object.keys(context.incomingReceiveAttempts).length) throw new Error("successful idempotency attempt was not cleared");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "p1-09c-49-incoming-single-flight.js")


def test_batch_partial_success_keeps_only_failed_rows_selected() -> None:
    body = _method_body("batchReceiveIncoming")
    assert "failedItemIds" in body
    assert "nextSelected" in body
    assert "this.incomingSelected = nextSelected" in body
    assert "this.incomingSelected = {}" not in body


def test_revert_is_single_flight_and_refresh_failure_keeps_success_message(
    tmp_path: Path,
) -> None:
    method_names = (
        "incomingOperationKey",
        "incomingOperationBusy",
        "incomingOperationIs",
        "revertIncoming",
    )
    methods = ",\n".join(_method_body(name).strip().rstrip(",") for name in method_names)
    script = f"""
const methods = {{
{methods}
}};
let resolveWrite;
let putCalls = 0;
globalThis.confirm = () => true;
globalThis.axios = {{
  put() {{
    putCalls += 1;
    return new Promise(resolve => {{ resolveWrite = resolve; }});
  }}
}};
const notices = [];
const row = {{receipt_item_id:17,item_id:"r8"}};
const context = {{
  incomingTab:"history",
  incomingOperationState:{{action:"",key:""}},
  loadIncomingHistory:async () => {{ throw new Error("刷新连接失败"); }},
  loadIncomingReceived:async () => true,
  loadIncoming:async () => true,
  loadKpi:async () => true,
  errorMessage:error => String(error?.message || error),
  showToast(message,danger) {{ notices.push({{message,danger:!!danger}}); }},
}};
for (const [name,method] of Object.entries(methods)) context[name] = method;
(async () => {{
  const first = methods.revertIncoming.call(context,row);
  const second = methods.revertIncoming.call(context,row);
  if (putCalls !== 1) throw new Error(`duplicate revert request: ${{putCalls}}`);
  resolveWrite({{data:{{ok:true}}}});
  const [firstResult,secondResult] = await Promise.all([first,second]);
  if (firstResult !== true || secondResult !== false) throw new Error("single-flight results are wrong");
  if (context.incomingOperationState.action) throw new Error("operation lock was not released");
  const success = notices.find(item => item.message.includes("撤销收料已成功") && item.message.includes("刷新失败"));
  if (!success || !success.danger) throw new Error("successful revert refresh warning is missing");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "p1-09c-49-incoming-revert-single-flight.js")


def test_incoming_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-49-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

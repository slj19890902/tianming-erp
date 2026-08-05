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


def test_production_tabs_expose_loading_error_retry_and_latest_request_contract() -> None:
    for marker in (
        "productionPendingState",
        "productionHistoryState",
        "正在读取待生产",
        "待生产读取失败",
        "正在读取完工历史",
        "完工历史读取失败",
        "refreshProductionTab",
        "selectProductionTab",
    ):
        assert marker in INDEX

    pending = _method_body("loadProduction")
    history = _method_body("loadProductionHistory")
    locations = _method_body("ensureProductionLocations")
    assert 'latestRequestControllers.get("production:pending") !== controller' in pending
    assert 'latestRequestControllers.get("production:history") !== controller' in history
    assert 'latestRequestControllers.get("production:locations") !== controller' in locations
    assert "this.productionPending = []" not in pending
    assert "this.productionHistory = []" not in history


def test_pending_latest_response_wins_even_when_old_request_finishes_last(
    tmp_path: Path,
) -> None:
    method = _method_body("loadProduction")
    script = f"""
const latestRequestControllers = new Map();
let requests = [];
globalThis.axios = {{
  get() {{ return new Promise(resolve => requests.push(resolve)); }}
}};
const methods = {{
{method}
}};
const loadProduction = methods.loadProduction;
const context = {{
  productionPending:[],
  productionLocations:[],
  productionSelected:{{}},
  productionPendingState:{{loaded:false,initialLoading:false,refreshing:false,error:""}},
  orders:[],
  beginLatestRequest(key) {{
    const previous = latestRequestControllers.get(key);
    if (previous) previous.abort();
    const controller = new AbortController();
    latestRequestControllers.set(key,controller);
    return controller;
  }},
  finishLatestRequest(key,controller) {{
    if (latestRequestControllers.get(key) === controller) latestRequestControllers.delete(key);
  }},
  isCancelledRequest() {{ return false; }},
  errorMessage(error) {{ return String(error?.message || error); }},
  productionLocationFloorNumber() {{ return null; }},
  productionLocationAreaCode() {{ return ""; }},
}};
(async () => {{
  const oldCall = loadProduction.call(context);
  const newCall = loadProduction.call(context);
  requests[1]({{data:{{items:[{{id:2,version:1,planned_quantity:2,material_input_quantity:2}}]}}}});
  const newResult = await newCall;
  requests[0]({{data:{{items:[{{id:1,version:1,planned_quantity:1,material_input_quantity:1}}]}}}});
  const oldResult = await oldCall;
  if (newResult !== true || oldResult !== false) throw new Error("request results do not expose latest/old state");
  if (context.productionPending.length !== 1 || context.productionPending[0].id !== 2) throw new Error("old response replaced latest rows");
  if (!context.productionPendingState.loaded || context.productionPendingState.initialLoading || context.productionPendingState.refreshing) throw new Error("loading state did not settle");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "p1-09c-50-production-latest-request.js")


def test_stock_transfer_write_success_is_not_reported_as_failure_when_refresh_fails(
    tmp_path: Path,
) -> None:
    methods = ",\n".join(
        _method_body(name).strip().rstrip(",")
        for name in ("productionLocation", "transferProductionCompletionToStock")
    )
    script = f"""
const methods = {{
{methods}
}};
let resolveWrite;
let postCalls = 0;
globalThis.confirm = () => true;
globalThis.createIdempotencyKey = () => "uat-transfer-key";
globalThis.axios = {{
  post() {{ postCalls += 1; return new Promise(resolve => {{ resolveWrite = resolve; }}); }}
}};
const notices = [];
const row = {{id:7,transfer_location_id:4,item_order_number:"UAT-PROD-001"}};
const context = {{
  productionBusy:false,
  productionAvailableLocations:[{{id:4,location_code:"C1-L01",pallet_id:null,pallet_code:null}}],
  productionTransferAttempts:{{}},
  productionTab:"history",
  loadProduction:async () => {{ throw new Error("刷新连接失败"); }},
  loadKpi:async () => true,
  loadDeliveries:async () => true,
  loadOverview:async () => true,
  hasPermission:() => false,
  errorMessage:error => String(error?.message || error),
  showToast(message,danger) {{ notices.push({{message,danger:!!danger}}); }},
}};
for (const [name,method] of Object.entries(methods)) context[name] = method;
(async () => {{
  const first = methods.transferProductionCompletionToStock.call(context,row);
  const second = methods.transferProductionCompletionToStock.call(context,row);
  if (postCalls !== 1) throw new Error(`duplicate stock transfer request: ${{postCalls}}`);
  resolveWrite({{data:{{ok:true}}}});
  const [firstResult,secondResult] = await Promise.all([first,second]);
  if (firstResult !== true || secondResult !== false) throw new Error("single-flight results are wrong");
  const success = notices.find(item => item.message.includes("已转入成品库存") && item.message.includes("刷新失败") && item.message.includes("不要重复"));
  if (!success || !success.danger) throw new Error("successful transfer refresh warning is missing");
  if (context.productionTransferAttempts[row.id]) throw new Error("successful transfer attempt was not cleared");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "p1-09c-50-production-transfer-success.js")


def test_revert_write_success_is_not_reported_as_failure_when_refresh_fails(
    tmp_path: Path,
) -> None:
    method = _method_body("revertProductionCompletion")
    script = f"""
const methods = {{
{method}
}};
const revertProductionCompletion = methods.revertProductionCompletion;
let resolveWrite;
let postCalls = 0;
globalThis.confirm = () => true;
globalThis.axios = {{
  post() {{ postCalls += 1; return new Promise(resolve => {{ resolveWrite = resolve; }}); }}
}};
const notices = [];
const row = {{id:9,can_revert:true,item_order_number:"UAT-PROD-002"}};
const context = {{
  productionBusy:false,
  productionTransferAttempts:{{}},
  productionSupplementAttempts:{{}},
  productionTab:"history",
  user:{{role:"admin"}},
  loadProduction:async () => {{ throw new Error("刷新连接失败"); }},
  loadOrders:async () => true,
  loadKpi:async () => true,
  loadDeliveries:async () => true,
  loadOverview:async () => true,
  hasPermission:() => false,
  errorMessage:error => String(error?.message || error),
  showToast(message,danger) {{ notices.push({{message,danger:!!danger}}); }},
}};
(async () => {{
  const first = revertProductionCompletion.call(context,row);
  const second = revertProductionCompletion.call(context,row);
  if (postCalls !== 1) throw new Error(`duplicate revert request: ${{postCalls}}`);
  resolveWrite({{data:{{ok:true}}}});
  const [firstResult,secondResult] = await Promise.all([first,second]);
  if (firstResult !== true || secondResult !== false) throw new Error("single-flight results are wrong");
  const success = notices.find(item => item.message.includes("生产确认已撤销") && item.message.includes("刷新失败") && item.message.includes("不要重复"));
  if (!success || !success.danger) throw new Error("successful revert refresh warning is missing");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "p1-09c-50-production-revert-success.js")


def test_batch_refresh_warning_preserves_success_fact_and_failed_selection() -> None:
    body = _method_body("batchConfirmProduction")
    assert "succeededRows" in body
    assert "failedGroups" in body
    assert "页面刷新失败" in body
    assert "成功项不要重复提交" in body
    assert "delete this.productionSelected[row.id]" in body


def test_production_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-50-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

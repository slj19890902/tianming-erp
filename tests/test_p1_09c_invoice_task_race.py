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


def test_invoice_task_list_has_latest_request_and_retry_contract() -> None:
    block = INDEX.split("async loadInvoiceTasks(sessionContext = null) {", 1)[1].split(
        "async confirmFinanceStatement(row) {", 1
    )[0]
    for marker in (
        'const requestKey = "finance:invoice-tasks";',
        "this.beginLatestRequest(requestKey)",
        "signal:controller.signal",
        "const requestIsCurrent = () => (",
        "&& sessionIsCurrent()",
        "this.isCancelledRequest(error)",
        "this.finishLatestRequest(requestKey, controller)",
    ):
        assert marker in block

    invoice_view = INDEX[
        INDEX.index("v-else-if=\"financeView==='invoice_tasks'\"") :
        INDEX.index("<template v-else>", INDEX.index("v-else-if=\"financeView==='invoice_tasks'\""))
    ]
    assert ':disabled="invoiceTaskState.loading"' in invoice_view
    assert invoice_view.count(':disabled="invoiceTaskState.loading"') >= 4
    assert "重新查询" in invoice_view
    assert 'v-else-if="invoiceTaskState.error"' in invoice_view


def test_invoice_task_old_response_cannot_overwrite_latest_filters(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the invoice task race regression"

    body = _method_body(
        "async loadInvoiceTasks(sessionContext = null) {", "async confirmFinanceStatement(row) {"
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.latestRequestControllers = new Map();
const pending = [];
globalThis.axios = {{
  get(url, options) {{
    return new Promise((resolve, reject) => pending.push({{resolve, reject, options}}));
  }}
}};
const vm = {{
  canViewInvoiceTasks: true,
  authGeneration: 1,
  user: {{id:7}},
  invoiceTaskFilters: {{customer_id:1, statement_month:"2026-07", status:"draft"}},
  invoiceTasks: [{{task_number:"原列表"}}],
  invoiceTaskState: {{loading:false, error:""}},
  beginLatestRequest(key) {{
    latestRequestControllers.get(key)?.abort();
    const controller = new AbortController();
    latestRequestControllers.set(key, controller);
    return controller;
  }},
  finishLatestRequest(key, controller) {{
    if (latestRequestControllers.get(key) === controller) latestRequestControllers.delete(key);
  }},
  isCancelledRequest(error) {{
    return error?.name === "AbortError" || error?.code === "ERR_CANCELED";
  }},
  errorMessage(error) {{ return error?.message || String(error); }}
}};
vm.loadInvoiceTasks = new AsyncFunction("sessionContext", {json.dumps(body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const first = vm.loadInvoiceTasks();
  vm.invoiceTaskFilters = {{customer_id:2, statement_month:"2026-08", status:"ready"}};
  const second = vm.loadInvoiceTasks();

  if (pending[0].options.params.customer_id !== 1 || pending[1].options.params.customer_id !== 2) {{
    throw new Error("request filters were not frozen per query");
  }}
  pending[1].resolve({{data:{{items:[{{task_number:"最新任务"}}]}}}});
  await second;
  pending[0].resolve({{data:{{items:[{{task_number:"旧任务"}}]}}}});
  await first;
  if (vm.invoiceTasks.length !== 1 || vm.invoiceTasks[0].task_number !== "最新任务") {{
    throw new Error("old invoice task response overwrote latest filters");
  }}
  if (vm.invoiceTaskState.loading) throw new Error("latest loading state did not finish");

  const staleFailure = vm.loadInvoiceTasks();
  const latestAfterFailure = vm.loadInvoiceTasks();
  pending[3].resolve({{data:{{items:[{{task_number:"失败后的最新任务"}}]}}}});
  await latestAfterFailure;
  pending[2].reject(new Error("旧请求失败"));
  await staleFailure;
  if (vm.invoiceTasks.length !== 1 || vm.invoiceTasks[0].task_number !== "失败后的最新任务") {{
    throw new Error("stale failure cleared the latest invoice task list");
  }}
  if (vm.invoiceTaskState.error) throw new Error("stale failure exposed an obsolete error");

  const failed = vm.loadInvoiceTasks();
  pending[4].reject(new Error("网络断开"));
  await failed;
  if (vm.invoiceTasks.length !== 0) throw new Error("current failure retained stale tasks");
  if (!vm.invoiceTaskState.error.includes("网络断开")) throw new Error("current failure was not shown");
  if (vm.invoiceTaskState.loading) throw new Error("current failure did not finish loading");

  vm.invoiceTaskFilters = {{customer_id:"", statement_month:"", status:""}};
  const blankFilters = vm.loadInvoiceTasks();
  const blankParams = pending[5].options.params;
  if (blankParams.customer_id !== undefined || blankParams.statement_month !== undefined || blankParams.status !== undefined) {{
    throw new Error("blank invoice task filters were sent as invalid query values");
  }}
  pending[5].resolve({{data:{{items:[]}}}});
  await blankFilters;
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "invoice-task-race.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_invoice_task_frontend_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend syntax check"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "invoice-task-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

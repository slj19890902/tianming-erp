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


def test_finance_statement_actions_show_busy_labels_and_disable_parallel_actions() -> None:
    finance = INDEX[
        INDEX.index("<template v-if=\"financeView==='current'\"") :
        INDEX.index("<template v-else-if=\"financeView==='reports'\"")
    ]
    assert "financeStatementOperationState.action" in finance
    assert "financeStatementOperationState.statementId" in finance
    assert "确认中…" in finance
    assert "生成中…" in finance
    assert finance.count(":disabled=") >= 2


def test_confirm_and_generate_invoice_task_are_single_flight_and_freeze_payload(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for finance action regression"
    confirm_body = _method_body(
        "async confirmFinanceStatement(row) {", "async generateInvoiceTask(row) {"
    )
    generate_body = _method_body(
        "async generateInvoiceTask(row) {", "async openInvoiceTask(task) {"
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
let idempotencyCalls = 0;
globalThis.createIdempotencyKey = () => `KEY-${{++idempotencyCalls}}`;
globalThis.axios = {{
  post(url, payload) {{
    return new Promise((resolve, reject) => pending.push({{url, payload, resolve, reject}}));
  }}
}};
let financeLoads = 0;
let taskLoads = 0;
const messages = [];
const vm = {{
  financeView:"current",
  financeStatementOperationState:{{action:"",statementId:null}},
  async loadFinance() {{ financeLoads += 1; return true; }},
  async loadInvoiceTasks() {{ taskLoads += 1; return true; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }}
}};
vm.confirmFinanceStatement = new AsyncFunction("row", {json.dumps(confirm_body, ensure_ascii=False)}).bind(vm);
vm.generateInvoiceTask = new AsyncFunction("row", {json.dumps(generate_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const statement = {{id:11,version:3}};
  const confirming = vm.confirmFinanceStatement(statement);
  const duplicateConfirmPromise = vm.confirmFinanceStatement(statement);
  await Promise.resolve();
  statement.version = 99;
  if (pending.length !== 1 || pending[0].url !== "/api/finance/statements/11/confirm" || pending[0].payload.expected_version !== 3) {{
    throw new Error("confirmation duplicated or did not freeze its payload");
  }}
  if (await duplicateConfirmPromise !== false) throw new Error("duplicate confirmation was not rejected");
  pending[0].resolve({{data:{{ok:true}}}});
  if (await confirming !== true || financeLoads !== 1 || vm.financeStatementOperationState.action) {{
    throw new Error("confirmation did not refresh and unlock");
  }}

  statement.version = 4;
  const generating = vm.generateInvoiceTask(statement);
  const duplicateGeneratePromise = vm.generateInvoiceTask(statement);
  await Promise.resolve();
  statement.version = 100;
  if (pending.length !== 2 || pending[1].url !== "/api/finance/statements/11/invoice-tasks" || pending[1].payload.expected_version !== 4) {{
    throw new Error("task generation duplicated or did not freeze its version");
  }}
  if (pending[1].payload.idempotency_key !== "KEY-1" || idempotencyCalls !== 1) throw new Error("task generation did not freeze one idempotency key");
  if (await duplicateGeneratePromise !== false) throw new Error("duplicate task generation was not rejected");
  pending[1].resolve({{data:{{id:88}}}});
  if (await generating !== true || taskLoads !== 1 || vm.financeView !== "invoice_tasks" || vm.financeStatementOperationState.action) {{
    throw new Error("task generation did not navigate, refresh and unlock");
  }}

  vm.financeView = "current";
  const failed = vm.confirmFinanceStatement({{id:12,version:2}});
  pending[2].reject(new Error("版本冲突"));
  if (await failed !== false || vm.financeStatementOperationState.action) throw new Error("failed confirmation did not unlock");
  if (!messages.some(row => row.danger && row.message.includes("版本冲突"))) throw new Error("failure was not explained");

  vm.loadFinance = async () => {{ throw new Error("刷新断开"); }};
  const confirmedButRefreshFailed = vm.confirmFinanceStatement({{id:13,version:5}});
  pending[3].resolve({{data:{{ok:true}}}});
  if (await confirmedButRefreshFailed !== true || vm.financeStatementOperationState.action) throw new Error("successful confirmation was misreported after refresh failure");
  if (!messages.some(row => row.danger && row.message.includes("已经确认") && row.message.includes("不要重复确认"))) throw new Error("confirmation refresh failure lacked anti-repeat guidance");

  vm.loadInvoiceTasks = async () => {{ throw new Error("列表断开"); }};
  const generatedButRefreshFailed = vm.generateInvoiceTask({{id:13,version:6}});
  pending[4].resolve({{data:{{id:99}}}});
  if (await generatedButRefreshFailed !== true || vm.financeStatementOperationState.action) throw new Error("successful task generation was misreported after refresh failure");
  if (!messages.some(row => row.danger && row.message.includes("已经生成") && row.message.includes("不要重复生成"))) throw new Error("generation refresh failure lacked anti-repeat guidance");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "finance-statement-actions.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_finance_statement_action_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend syntax check"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "finance-statement-actions-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

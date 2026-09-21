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
    assert node is not None, "Node.js is required for the invoice task action regression"
    target = tmp_path / name
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_invoice_task_actions_expose_loading_retry_and_single_flight_ui() -> None:
    view = INDEX[
        INDEX.index("v-else-if=\"financeView==='invoice_tasks'\"") :
        INDEX.index("<template v-else>", INDEX.index("v-else-if=\"financeView==='invoice_tasks'\""))
    ]
    detail = INDEX[
        INDEX.index("v-else-if=\"modal.type === 'invoiceTask'\"") :
        INDEX.index("v-else-if=\"modal.type === 'invoiceTaskResult'\"")
    ]
    result = INDEX[
        INDEX.index("v-else-if=\"modal.type === 'invoiceTaskResult'\"") :
        INDEX.index("v-else-if=\"modal.type === 'invoiceSellers'\"")
    ]

    assert "invoiceTaskOperationState.detailLoading" in detail
    assert "invoiceTaskOperationState.detailError" in detail
    assert "retryInvoiceTaskDetail" in detail
    assert "重新读取" in detail
    assert "读取中…" in view
    assert "确认中…" in view
    assert "登记中…" in result
    assert ':disabled="invoiceTaskOperationState.resultSaving"' in result
    assert '<fieldset :disabled="invoiceTaskOperationState.resultSaving"' in result


def test_invoice_task_preview_opens_immediately_and_discards_stale_responses(
    tmp_path: Path,
) -> None:
    open_body = _method_body(
        "async openInvoiceTask(task) {", "async loadInvoiceTaskDetail(target = null) {"
    )
    load_body = _method_body(
        "async loadInvoiceTaskDetail(target = null) {", "async retryInvoiceTaskDetail() {"
    )
    retry_body = _method_body(
        "async retryInvoiceTaskDetail() {", "async confirmInvoiceTask(task) {"
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.latestRequestControllers = new Map();
const pending = [];
globalThis.axios = {{
  get(url, options) {{
    return new Promise((resolve, reject) => pending.push({{url, options, resolve, reject}}));
  }}
}};
const vm = {{
  modal:null,
  invoiceTaskDetail:{{id:999}},
  invoiceTaskOperationState:{{detailLoading:false,detailError:"",detailTaskId:null,detailTaskNumber:"",confirmingTaskId:null,resultSaving:false}},
  beginLatestRequest(key) {{
    latestRequestControllers.get(key)?.abort();
    const controller = new AbortController();
    latestRequestControllers.set(key, controller);
    return controller;
  }},
  finishLatestRequest(key, controller) {{
    if (latestRequestControllers.get(key) === controller) latestRequestControllers.delete(key);
  }},
  isCancelledRequest(error) {{ return error?.name === "AbortError" || error?.code === "ERR_CANCELED"; }},
  errorMessage(error) {{ return error?.message || String(error); }}
}};
vm.openInvoiceTask = new AsyncFunction("task", {json.dumps(open_body, ensure_ascii=False)}).bind(vm);
vm.loadInvoiceTaskDetail = new AsyncFunction("target", {json.dumps(load_body, ensure_ascii=False)}).bind(vm);
vm.retryInvoiceTaskDetail = new AsyncFunction({json.dumps(retry_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const first = vm.openInvoiceTask({{id:1,task_number:"TASK-A"}});
  if (vm.modal?.type !== "invoiceTask" || !vm.invoiceTaskOperationState.detailLoading || vm.invoiceTaskDetail !== null) {{
    throw new Error("preview did not open its loading modal immediately");
  }}
  const second = vm.openInvoiceTask({{id:2,task_number:"TASK-B"}});
  if (pending.length !== 2 || !pending[0].options.signal || !pending[1].options.signal) throw new Error("preview requests lack cancellation signals");
  pending[1].resolve({{data:{{id:2,customer_name:"客户B"}}}});
  await second;
  pending[0].resolve({{data:{{id:1,customer_name:"客户A"}}}});
  await first;
  if (vm.invoiceTaskDetail?.id !== 2 || vm.invoiceTaskOperationState.detailTaskId !== 2) {{
    throw new Error("stale preview overwrote the latest task");
  }}

  const failed = vm.openInvoiceTask({{id:3,task_number:"TASK-C"}});
  pending[2].reject(new Error("网络断开"));
  await failed;
  if (vm.invoiceTaskDetail !== null || !vm.invoiceTaskOperationState.detailError.includes("网络断开")) {{
    throw new Error("current preview failure was not kept in the modal");
  }}
  if (vm.invoiceTaskOperationState.detailLoading) throw new Error("failed preview remained loading");

  const retried = vm.retryInvoiceTaskDetail();
  pending[3].resolve({{data:{{id:3,customer_name:"客户C"}}}});
  await retried;
  if (vm.invoiceTaskDetail?.id !== 3 || vm.invoiceTaskOperationState.detailError) {{
    throw new Error("preview retry did not recover the same task");
  }}
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "invoice-task-preview-actions.js", script)


def test_invoice_task_confirm_and_result_save_are_single_flight(tmp_path: Path) -> None:
    confirm_body = _method_body(
        "async confirmInvoiceTask(task) {", "async downloadInvoiceTaxTemplate(task) {"
    )
    save_body = _method_body(
        "async saveInvoiceTaskResult() {", "uploadInvoiceTaskPdf(task) {"
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
globalThis.axios = {{
  post(url, payload) {{
    return new Promise((resolve, reject) => pending.push({{url, payload, resolve, reject}}));
  }}
}};
const messages = [];
const vm = {{
  modal:null,
  invoiceTaskOperationState:{{detailLoading:false,detailError:"",detailTaskId:null,detailTaskNumber:"",confirmingTaskId:null,resultSaving:false}},
  invoiceTaskResult:{{task_id:20,status:"issued",invoice_number:"INV-001",invoice_date:"2026-08-05",failure_reason:"",expected_version:7,expected_ledger_version:4}},
  async loadInvoiceTasks() {{ return true; }},
  closeModal() {{ this.modal = null; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }}
}};
vm.confirmInvoiceTask = new AsyncFunction("task", {json.dumps(confirm_body, ensure_ascii=False)}).bind(vm);
vm.saveInvoiceTaskResult = new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const task = {{id:10,version:3}};
  const confirming = vm.confirmInvoiceTask(task);
  const duplicateConfirmPromise = vm.confirmInvoiceTask(task);
  await Promise.resolve();
  task.version = 99;
  if (pending.length !== 1 || pending[0].payload.expected_version !== 3) {{
    throw new Error("confirm action was duplicated or did not freeze its version");
  }}
  const duplicateConfirm = await duplicateConfirmPromise;
  if (duplicateConfirm !== false) throw new Error("duplicate confirmation was not rejected");
  pending[0].resolve({{data:{{ok:true}}}});
  await confirming;
  if (vm.invoiceTaskOperationState.confirmingTaskId !== null) throw new Error("confirm lock was not released");

  vm.modal = {{type:"invoiceTaskResult"}};
  const saving = vm.saveInvoiceTaskResult();
  const duplicateSavePromise = vm.saveInvoiceTaskResult();
  await Promise.resolve();
  vm.invoiceTaskResult.invoice_number = "CHANGED";
  vm.invoiceTaskResult.expected_version = 99;
  vm.invoiceTaskResult.expected_ledger_version = 99;
  if (pending.length !== 2 || pending[1].payload.invoice_number !== "INV-001" || pending[1].payload.expected_version !== 7 || pending[1].payload.expected_ledger_version !== 4) {{
    throw new Error("result registration was duplicated or did not freeze its form");
  }}
  const duplicateSave = await duplicateSavePromise;
  if (duplicateSave !== false) throw new Error("duplicate result registration was not rejected");
  pending[1].resolve({{data:{{ok:true}}}});
  await saving;
  if (vm.modal !== null || vm.invoiceTaskOperationState.resultSaving) throw new Error("successful result registration did not close and unlock");

  vm.modal = {{type:"invoiceTaskResult"}};
  vm.invoiceTaskResult = {{task_id:21,status:"failed",invoice_number:"",invoice_date:"",failure_reason:"税局拒绝",expected_version:2,expected_ledger_version:0}};
  const failed = vm.saveInvoiceTaskResult();
  pending[2].reject(new Error("服务器忙"));
  await failed;
  if (vm.modal?.type !== "invoiceTaskResult" || vm.invoiceTaskOperationState.resultSaving) {{
    throw new Error("failed result registration did not retain the form and release the lock");
  }}
  if (!messages.some(row => row.danger && row.message.includes("服务器忙"))) throw new Error("failed result was not explained");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "invoice-task-submit-actions.js", script)


def test_invoice_task_result_freezes_statement_ledger_version() -> None:
    assert "expected_ledger_version:Number(task.ledger_version || 0)" in INDEX
    save = INDEX[
        INDEX.index("async saveInvoiceTaskResult() {") :
        INDEX.index("uploadInvoiceTaskPdf(task) {")
    ]
    assert "expected_ledger_version:Number(form.expected_ledger_version || 0)" in save
    assert "expected_ledger_version:submitted.status === \"issued\"" in save


def test_invoice_task_action_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend syntax check"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "invoice-task-actions-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

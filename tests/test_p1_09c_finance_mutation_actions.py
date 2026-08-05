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


def test_finance_mutation_buttons_share_busy_state_and_hide_legacy_invoice_after_confirm() -> None:
    finance = INDEX[
        INDEX.index("<template v-if=\"financeView==='current'\"") :
        INDEX.index("<template v-else-if=\"financeView==='reports'\"")
    ]
    for label in ("取消中…", "登记中…", "核销中…"):
        assert label in finance
    assert "financeStatementOperationState.action==='cancel'" in finance
    assert "financeStatementOperationState.action==='register'" in finance
    assert "financeStatementOperationState.action==='settle'" in finance
    assert "canFinance && statement.confirmation_status!=='confirmed'" in finance


def test_cancel_register_and_settle_are_single_flight_and_freeze_payload(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for finance mutation regression"
    cancel_body = _method_body("async cancelStatement(row) {", "closeModal() {")
    register_body = _method_body("async registerInvoice(row) {", "async settle(row) {")
    settle_body = _method_body("async settle(row) {", "beginBackupAction(action) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
const prompts = [];
let confirmAnswer = true;
globalThis.confirm = () => confirmAnswer;
globalThis.prompt = () => prompts.shift();
globalThis.today = () => "2026-08-05";
globalThis.axios = {{
  post(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"post",url,payload,resolve,reject}})); }},
  put(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"put",url,payload,resolve,reject}})); }}
}};
let financeLoads = 0;
let kpiLoads = 0;
const messages = [];
const vm = {{
  modal:{{type:"statementDetail"}},
  statementDetail:{{id:1}},
  financeStatementOperationState:{{action:"",statementId:null}},
  async loadFinance() {{ financeLoads += 1; return true; }},
  async loadKpi() {{ kpiLoads += 1; return true; }},
  normalizeValidationErrors() {{ return ""; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }}
}};
vm.cancelStatement = new AsyncFunction("row", {json.dumps(cancel_body, ensure_ascii=False)}).bind(vm);
vm.registerInvoice = new AsyncFunction("row", {json.dumps(register_body, ensure_ascii=False)}).bind(vm);
vm.settle = new AsyncFunction("row", {json.dumps(settle_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  confirmAnswer = false;
  if (await vm.cancelStatement({{id:1}}) !== false || pending.length || vm.financeStatementOperationState.action) throw new Error("cancelled confirmation had side effects");
  confirmAnswer = true;
  const cancelling = vm.cancelStatement({{id:1}});
  const duplicateCancelPromise = vm.cancelStatement({{id:2}});
  await Promise.resolve();
  if (pending.length !== 1 || pending[0].url !== "/api/finance/statements/1/cancel") throw new Error("cancel request duplicated or changed target");
  if (await duplicateCancelPromise !== false) throw new Error("duplicate cancel was not rejected");
  pending[0].resolve({{data:{{ok:true}}}});
  if (await cancelling !== true || vm.financeStatementOperationState.action || vm.modal !== null || vm.statementDetail !== null) throw new Error("cancel did not close, refresh and unlock");

  prompts.push(" INV-001 ", "75.50");
  const invoiceRow = {{id:2,total_receivable:100,invoiced_amount:20,pending_invoice_amount:80}};
  const registering = vm.registerInvoice(invoiceRow);
  const duplicateRegisterPromise = vm.registerInvoice(invoiceRow);
  await Promise.resolve();
  invoiceRow.id = 99; invoiceRow.invoiced_amount = 99;
  if (pending.length !== 2 || pending[1].url !== "/api/finance/invoices" || pending[1].payload.statement_id !== 2 || pending[1].payload.invoice_number !== "INV-001" || pending[1].payload.invoice_amount !== "75.50") {{
    throw new Error("invoice registration duplicated or did not freeze its payload");
  }}
  if (await duplicateRegisterPromise !== false) throw new Error("duplicate invoice registration was not rejected");
  pending[1].resolve({{data:{{id:20}}}});
  if (await registering !== true || vm.financeStatementOperationState.action) throw new Error("invoice registration did not refresh and unlock");

  prompts.push("60.25");
  const settleRow = {{id:3,total_receivable:100,settled_amount:10,pending_payment_amount:90}};
  const settling = vm.settle(settleRow);
  const duplicateSettlePromise = vm.settle(settleRow);
  await Promise.resolve();
  settleRow.id = 98; settleRow.settled_amount = 99;
  if (pending.length !== 3 || pending[2].url !== "/api/finance/statements/3/settle" || pending[2].payload.amount !== "60.25" || pending[2].payload.settlement_date !== "2026-08-05") {{
    throw new Error("settlement duplicated or did not freeze its payload");
  }}
  if (await duplicateSettlePromise !== false) throw new Error("duplicate settlement was not rejected");
  pending[2].resolve({{data:{{ok:true}}}});
  if (await settling !== true || vm.financeStatementOperationState.action || kpiLoads !== 1) throw new Error("settlement did not refresh and unlock");

  prompts.push("INV-002", "10.00");
  vm.loadFinance = async () => {{ throw new Error("刷新断开"); }};
  const registeredButRefreshFailed = vm.registerInvoice({{id:4,total_receivable:20,invoiced_amount:0,pending_invoice_amount:20}});
  pending[3].resolve({{data:{{id:21}}}});
  if (await registeredButRefreshFailed !== true || vm.financeStatementOperationState.action) throw new Error("successful registration was misreported after refresh failure");
  if (!messages.some(row => row.danger && row.message.includes("已经登记") && row.message.includes("不要重复登记"))) throw new Error("registration refresh failure lacked anti-repeat guidance");

  prompts.push("5.00");
  const failedSettlement = vm.settle({{id:5,total_receivable:20,settled_amount:0,pending_payment_amount:20}});
  pending[4].reject(new Error("金额已变化"));
  if (await failedSettlement !== false || vm.financeStatementOperationState.action) throw new Error("failed settlement did not unlock");
  if (!messages.some(row => row.danger && row.message.includes("金额已变化"))) throw new Error("settlement failure was not explained");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "finance-mutation-actions.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_finance_mutation_action_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend syntax check"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "finance-mutation-actions-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

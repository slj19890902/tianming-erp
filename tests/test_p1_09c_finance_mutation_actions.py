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


def test_active_finance_buttons_use_statement_task_flow_without_receipt_action() -> None:
    finance = INDEX[
        INDEX.index("<template v-if=\"financeView==='current'\"") :
        INDEX.index("<template v-else-if=\"financeView==='reports'\"")
    ]
    assert "financeStatementOperationState.action==='export_confirm'" in finance
    assert "financeStatementOperationState.action==='generate'" in finance
    for label in ("导出并确认", "生成中…", "客户异议", "仅导出", "PDF", "确认收款"):
        assert label in finance
    assert "确认收款" in finance


def test_cancel_register_and_settle_are_single_flight_and_freeze_payload(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for finance mutation regression"
    cancel_body = _method_body("async cancelStatement(row) {", "closeModal() {")
    helper_source = INDEX[
        INDEX.index("financeManualMutationStorageKey(actorId) {") :
        INDEX.index("async registerInvoice(row) {")
    ].strip().rstrip(",")
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
let idempotencyCalls = 0;
globalThis.createIdempotencyKey = () => `KEY-${{String(++idempotencyCalls).padStart(8,"0")}}`;
const stored = new Map();
globalThis.sessionStorage = {{
  getItem(key) {{ return stored.has(key) ? stored.get(key) : null; }},
  setItem(key, value) {{ stored.set(key, String(value)); }},
  removeItem(key) {{ stored.delete(key); }},
}};
globalThis.axios = {{
  post(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"post",url,payload,resolve,reject}})); }},
  put(url, payload) {{ return new Promise((resolve, reject) => pending.push({{method:"put",url,payload,resolve,reject}})); }}
}};
let financeLoads = 0;
let kpiLoads = 0;
const messages = [];
const vm = {{
  user:{{id:1}},
  authGeneration:0,
  modal:{{type:"statementDetail"}},
  statementDetail:{{id:1}},
  financeStatementOperationState:{{action:"",statementId:null,actorId:null,authGeneration:null,idempotencyKey:""}},
  financeManualMutationAttempts:{{register:null,settle:null}},
  financeManualMutationRecoveryError:"",
  async loadFinance() {{ financeLoads += 1; return true; }},
  async loadKpi() {{ kpiLoads += 1; return true; }},
  normalizeValidationErrors() {{ return ""; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }}
}};
Object.assign(vm, ({{{helper_source}}}));
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
  const invoiceRow = {{id:2,total_receivable:100,invoiced_amount:20,pending_invoice_amount:80,confirmation_status:"confirmed",version:4,ledger_version:6}};
  const registering = vm.registerInvoice(invoiceRow);
  const duplicateRegisterPromise = vm.registerInvoice(invoiceRow);
  await Promise.resolve();
  invoiceRow.id = 99; invoiceRow.invoiced_amount = 99;
  if (pending.length !== 2 || pending[1].url !== "/api/finance/invoices" || pending[1].payload.statement_id !== 2 || pending[1].payload.invoice_number !== "INV-001" || pending[1].payload.invoice_amount !== "75.50" || pending[1].payload.expected_version !== 4 || pending[1].payload.expected_ledger_version !== 6 || pending[1].payload.idempotency_key !== "KEY-00000001") {{
    throw new Error("invoice registration duplicated or did not freeze its payload");
  }}
  if (await duplicateRegisterPromise !== false) throw new Error("duplicate invoice registration was not rejected");
  pending[1].resolve({{data:{{id:20}}}});
  if (await registering !== true || vm.financeStatementOperationState.action) throw new Error("invoice registration did not refresh and unlock");
  if (vm.financeManualMutationAttempts.register) throw new Error("successful invoice did not clear its attempt");

  prompts.push("60.25");
  const settleRow = {{id:3,total_receivable:100,settled_amount:10,pending_payment_amount:90,confirmation_status:"confirmed",version:7,ledger_version:8}};
  const settling = vm.settle(settleRow);
  const duplicateSettlePromise = vm.settle(settleRow);
  await Promise.resolve();
  settleRow.id = 98; settleRow.settled_amount = 99;
  if (pending.length !== 3 || pending[2].url !== "/api/finance/statements/3/settle" || pending[2].payload.amount !== "60.25" || pending[2].payload.settlement_date !== "2026-08-05" || pending[2].payload.expected_version !== 7 || pending[2].payload.expected_ledger_version !== 8 || pending[2].payload.idempotency_key !== "KEY-00000002") {{
    throw new Error("settlement duplicated or did not freeze its payload");
  }}
  if (await duplicateSettlePromise !== false) throw new Error("duplicate settlement was not rejected");
  pending[2].resolve({{data:{{ok:true}}}});
  if (await settling !== true || vm.financeStatementOperationState.action || kpiLoads !== 1) throw new Error("settlement did not refresh and unlock");
  if (vm.financeManualMutationAttempts.settle) throw new Error("successful settlement did not clear its attempt");

  prompts.push("INV-002", "10.00");
  vm.loadFinance = async () => {{ throw new Error("刷新断开"); }};
  const registeredButRefreshFailed = vm.registerInvoice({{id:4,total_receivable:20,invoiced_amount:0,pending_invoice_amount:20,confirmation_status:"confirmed",version:2,ledger_version:1}});
  pending[3].resolve({{data:{{id:21}}}});
  if (await registeredButRefreshFailed !== true || vm.financeStatementOperationState.action) throw new Error("successful registration was misreported after refresh failure");
  if (vm.financeManualMutationAttempts.register) throw new Error("confirmed success retained an invoice attempt");
  if (!messages.some(row => row.danger && row.message.includes("已经登记") && row.message.includes("不要重复登记"))) throw new Error("registration refresh failure lacked anti-repeat guidance");

  prompts.push("5.00");
  const uncertainRow = {{id:5,total_receivable:20,settled_amount:0,pending_payment_amount:20,confirmation_status:"confirmed",version:9,ledger_version:11}};
  const failedSettlement = vm.settle(uncertainRow);
  pending[4].reject(new Error("金额已变化"));
  if (await failedSettlement !== false || vm.financeStatementOperationState.action) throw new Error("failed settlement did not unlock");
  if (!messages.some(row => row.danger && row.message.includes("结果暂未确认"))) throw new Error("uncertain settlement was not explained");
  const retainedKey = vm.financeManualMutationAttempts.settle?.payload?.idempotency_key;
  if (retainedKey !== "KEY-00000004") throw new Error("uncertain settlement did not retain its key");
  const retrySettlement = vm.settle(uncertainRow);
  await Promise.resolve();
  if (pending[5].payload.idempotency_key !== retainedKey || pending[5].payload.expected_version !== 9 || pending[5].payload.expected_ledger_version !== 11 || prompts.length) throw new Error("uncertain settlement did not retry its frozen request");
  pending[5].resolve({{data:{{ok:true}}}});
  if (await retrySettlement !== true || vm.financeManualMutationAttempts.settle) throw new Error("successful retry did not clear the retained attempt");

  prompts.push("6.00");
  const rejectedSettlement = vm.settle({{id:6,total_receivable:20,settled_amount:0,pending_payment_amount:20,confirmation_status:"confirmed",version:3,ledger_version:4}});
  pending[6].reject({{message:"版本冲突",response:{{status:409}}}});
  if (await rejectedSettlement !== false || vm.financeManualMutationAttempts.settle) throw new Error("definite rejection did not clear its attempt");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "finance-mutation-actions.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_uncertain_finance_attempt_restores_only_for_same_actor(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for finance session regression"
    helper_source = INDEX[
        INDEX.index("financeManualMutationStorageKey(actorId) {") :
        INDEX.index("async registerInvoice(row) {")
    ].strip().rstrip(",")
    script = f"""
const stored = new Map();
globalThis.sessionStorage = {{
  getItem(key) {{ return stored.has(key) ? stored.get(key) : null; }},
  setItem(key, value) {{ stored.set(key, String(value)); }},
  removeItem(key) {{ stored.delete(key); }},
}};
const helpers = ({{{helper_source}}});
const makeVm = (actorId, authGeneration) => Object.assign({{
  user:{{id:actorId}}, authGeneration,
  financeManualMutationAttempts:{{register:null,settle:null}},
  financeManualMutationRecoveryError:"",
  financeStatementOperationState:{{action:"",statementId:null,actorId:null,authGeneration:null,idempotencyKey:""}},
}}, helpers);

const first = makeVm(11, 3);
const attempt = first.setFinanceManualMutationAttempt("register", {{
  statementId:72,
  payload:{{
    statement_id:72, invoice_number:"INV-RELOAD", invoice_date:"2026-08-27",
    invoice_amount:"88.00", expected_version:2, expected_ledger_version:5,
    idempotency_key:"same-actor-reload-key",
  }},
}});
if (!attempt) throw new Error("initial attempt was not persisted");
const reloaded = makeVm(11, 9);
if (!reloaded.restoreFinanceManualMutationAttemptsForCurrentUser()) throw new Error("same actor restore failed");
const restored = reloaded.financeManualMutationAttempts.register;
if (!restored || restored.actorId !== 11 || restored.authGeneration !== 9) throw new Error("same actor metadata was not rebound");
if (restored.payload.idempotency_key !== "same-actor-reload-key" || restored.payload.expected_version !== 2 || restored.payload.expected_ledger_version !== 5) throw new Error("frozen source and ledger payload changed on reload");

reloaded.clearFinanceManualMutationSessionState();
if (reloaded.financeManualMutationAttempts.register) throw new Error("logout-style memory clear failed");
const switched = makeVm(22, 10);
if (!switched.restoreFinanceManualMutationAttemptsForCurrentUser()) throw new Error("new actor empty restore failed");
if (switched.financeManualMutationAttempts.register || switched.financeManualMutationAttempts.settle) throw new Error("another actor hydrated the previous actor payload");
const sameActorAgain = makeVm(11, 11);
sameActorAgain.restoreFinanceManualMutationAttemptsForCurrentUser();
if (sameActorAgain.financeManualMutationAttempts.register?.payload?.idempotency_key !== "same-actor-reload-key") throw new Error("logout discarded the original actor recovery key");

stored.set("erp_finance_manual_mutation_attempts_v1:11", JSON.stringify({{
  register:{{
    actorId:11, statementId:72,
    payload:{{
      statement_id:999, invoice_number:"INV-CORRUPT", invoice_date:"2026-08-27",
      invoice_amount:"88.00", expected_version:2, expected_ledger_version:5,
      idempotency_key:"corrupt-target-key",
    }},
  }},
}}));
const corrupt = makeVm(11, 12);
if (corrupt.restoreFinanceManualMutationAttemptsForCurrentUser() !== false || !corrupt.financeManualMutationRecoveryError) throw new Error("mismatched statement target was restored as a safe finance retry");
"""
    target = tmp_path / "finance-same-actor-reload.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_finance_attempt_ignores_stale_actor_callback_and_classifies_ambiguous_statuses(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for finance session regression"
    helper_source = INDEX[
        INDEX.index("financeManualMutationStorageKey(actorId) {") :
        INDEX.index("async registerInvoice(row) {")
    ].strip().rstrip(",")
    settle_body = _method_body("async settle(row) {", "beginBackupAction(action) {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const helpers = ({{{helper_source}}});
const stored = new Map();
globalThis.sessionStorage = {{
  getItem(key) {{ return stored.has(key) ? stored.get(key) : null; }},
  setItem(key, value) {{ stored.set(key, String(value)); }},
  removeItem(key) {{ stored.delete(key); }},
}};
globalThis.today = () => "2026-08-27";
let idempotencyCalls = 0;
globalThis.createIdempotencyKey = () => `STATUS-${{String(++idempotencyCalls).padStart(8,"0")}}`;
const prompts = [];
globalThis.prompt = () => prompts.shift();
const pending = [];
globalThis.axios = {{
  put(url, payload) {{ return new Promise((resolve, reject) => pending.push({{url,payload,resolve,reject}})); }},
}};
const messages = [];
let financeLoads = 0;
let kpiLoads = 0;
const makeVm = (actorId, authGeneration=1) => {{
  const vm = Object.assign({{
    user:{{id:actorId}}, authGeneration,
    financeManualMutationAttempts:{{register:null,settle:null}},
    financeManualMutationRecoveryError:"",
    financeStatementOperationState:{{action:"",statementId:null,actorId:null,authGeneration:null,idempotencyKey:""}},
    async loadFinance() {{ financeLoads += 1; return true; }},
    async loadKpi() {{ kpiLoads += 1; return true; }},
    normalizeValidationErrors() {{ return ""; }},
    errorMessage(error) {{ return error?.message || String(error); }},
    showToast(message, danger=false) {{ messages.push({{message,danger,actorId:this.user?.id}}); }},
  }}, helpers);
  vm.settle = new AsyncFunction("row", {json.dumps(settle_body, ensure_ascii=False)}).bind(vm);
  return vm;
}};
const row = actorId => ({{
  id:actorId, total_receivable:20, settled_amount:0, pending_payment_amount:20,
  confirmation_status:"confirmed", version:2, ledger_version:1,
}});

(async () => {{
  const actorA = makeVm(101, 4);
  prompts.push("5.00");
  const started = actorA.settle(row(101));
  await Promise.resolve();
  const staleRequest = pending.shift();
  if (!staleRequest || staleRequest.payload.expected_ledger_version !== 1) throw new Error("actor A request was not started");
  actorA.authGeneration += 1;
  actorA.clearFinanceManualMutationSessionState();
  actorA.user = {{id:202}};
  const messageCount = messages.length;
  staleRequest.resolve({{data:{{ok:true}}}});
  if (await started !== false) throw new Error("stale actor callback reported success into the new session");
  if (messages.length !== messageCount || financeLoads || kpiLoads) throw new Error("stale callback changed the new actor UI");
  if (actorA.financeManualMutationAttempts.register || actorA.financeManualMutationAttempts.settle || actorA.financeStatementOperationState.action) throw new Error("stale callback restored old busy state");
  const recoveredA = makeVm(101, 6);
  recoveredA.restoreFinanceManualMutationAttemptsForCurrentUser();
  if (!recoveredA.financeManualMutationAttempts.settle) throw new Error("actor A lost the uncertain recovery key after actor switch");

  const ambiguous = [0,408,425,429,500,502,503,504];
  const definite = [400,401,403,404,409,422];
  for (const status of [...ambiguous, ...definite]) {{
    const actorId = 1000 + status;
    const vm = makeVm(actorId, 1);
    prompts.push("6.00");
    const action = vm.settle(row(actorId));
    await Promise.resolve();
    const request = pending.shift();
    if (!request) throw new Error(`missing status request ${{status}}`);
    request.reject(status ? {{message:`HTTP ${{status}}`,response:{{status}}}} : new Error("network disconnected"));
    if (await action !== false) throw new Error(`failed status ${{status}} reported success`);
    const retained = !!vm.financeManualMutationAttempts.settle;
    if (ambiguous.includes(status) !== retained) throw new Error(`status ${{status}} retention mismatch`);
    if (retained) vm.clearFinanceManualMutationAttempt("settle", vm.financeManualMutationAttempts.settle);
  }}
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "finance-actor-status-safety.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_finance_logout_and_auth_expiry_clear_only_in_memory_state() -> None:
    auth_required = INDEX[
        INDEX.index("window.erpAuthRequired = () => {") :
        INDEX.index("window.erpForbidden =", INDEX.index("window.erpAuthRequired = () => {"))
    ]
    logout = INDEX[
        INDEX.index("async logout() {") :
        INDEX.index("resetPagePerformanceState() {", INDEX.index("async logout() {"))
    ]
    assert auth_required.index("this.authGeneration += 1") < auth_required.index(
        "this.clearFinanceManualMutationSessionState()"
    ) < auth_required.index("this.user = null")
    assert logout.index("this.authGeneration += 1") < logout.index(
        "this.clearFinanceManualMutationSessionState()"
    ) < logout.index("this.user = null")
    assert "forgetStored:true" not in auth_required
    assert "forgetStored:true" not in logout


def test_finance_refresh_responses_cannot_cross_auth_generation(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for finance session isolation"
    load_finance_body = _method_body(
        "async loadFinance() {", "async loadFinanceOverview(sessionContext = null) {"
    )
    load_kpi_body = _method_body("async loadKpi() {", "async loadCustomers() {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.month = () => "2026-08";
const pending = [];
globalThis.axios = {{
  get(url, config) {{ return new Promise((resolve, reject) => pending.push({{url,config,resolve,reject}})); }},
}};
const vm = {{
  user:{{id:101}}, authGeneration:7, financeView:"current",
  pages:{{financeCurrent:1}}, pageSize:20,
  financeFilters:{{statement_month:"2026-08",balance_type:"",customer_id:""}},
  financeCurrentState:{{loading:false,error:"",loaded:false}},
  financeCurrentRows:[{{owner:"A-before"}}], financeCurrentTotal:1,
  financeCustomersTotal:1, financeCurrentSummary:{{owner:"A-before"}},
}};
vm.loadFinance = new AsyncFunction({json.dumps(load_finance_body, ensure_ascii=False)}).bind(vm);
vm.loadKpi = new AsyncFunction({json.dumps(load_kpi_body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const financeRequest = vm.loadFinance();
  const kpiRequest = vm.loadKpi();
  await Promise.resolve();
  if (pending.length !== 2) throw new Error("expected finance and KPI refresh requests");

  vm.authGeneration = 8;
  vm.user = {{id:202}};
  vm.financeCurrentRows = [{{owner:"B"}}];
  vm.financeCurrentTotal = 9;
  vm.financeCustomersTotal = 9;
  vm.financeCurrentSummary = {{owner:"B"}};
  vm.financeCurrentState = {{loading:true,error:"B-loading",loaded:true}};
  vm.kpi = {{owner:"B"}};

  pending.find(item => item.url.includes("current-customer-months")).resolve({{data:{{
    items:[{{owner:"A-late"}}], total:99, summary:{{owner:"A-late"}},
  }}}});
  pending.find(item => item.url.includes("dashboard/kpi")).resolve({{data:{{owner:"A-late"}}}});

  if (await financeRequest !== false || await kpiRequest !== false) throw new Error("stale refresh reported success");
  if (vm.financeCurrentRows[0].owner !== "B" || vm.financeCurrentTotal !== 9 || vm.financeCustomersTotal !== 9) throw new Error("late finance response overwrote actor B rows");
  if (vm.financeCurrentSummary.owner !== "B" || vm.kpi.owner !== "B") throw new Error("late refresh overwrote actor B summary or KPI");
  if (vm.financeCurrentState.loading !== true || vm.financeCurrentState.error !== "B-loading" || vm.financeCurrentState.loaded !== true) throw new Error("late finance finally/catch overwrote actor B loading state");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "finance-refresh-auth-generation.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_finance_overview_and_payables_keep_only_latest_session_request(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for finance latest-request isolation"
    load_finance_body = _method_body(
        "async loadFinance() {", "async loadFinanceOverview(sessionContext = null) {"
    )
    overview_body = _method_body(
        "async loadFinanceOverview(sessionContext = null) {", "financeTrendHeight(value) {"
    )
    payables_body = _method_body(
        "async loadFinancePayables(sessionContext = null) {", "applyPayableSupplier() {"
    )
    load_finance_source = INDEX[
        INDEX.index("async loadFinance() {") : INDEX.index(
            "async loadFinanceOverview(sessionContext = null) {"
        )
    ]
    assert (
        "this.loadFinanceOverview({authGeneration:requestAuthGeneration,userId:requestUserId})"
        in load_finance_source
    )
    assert (
        "this.loadFinancePayables({authGeneration:requestAuthGeneration,userId:requestUserId})"
        in load_finance_source
    )

    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.month = () => "2026-08";
globalThis.latestRequestControllers = new Map();
const pending = [];
globalThis.axios = {{
  get(url, config={{}}) {{
    return new Promise((resolve, reject) => pending.push({{url,config,resolve,reject}}));
  }},
}};
const vm = {{
  user:{{id:101}}, authGeneration:7, financeView:"overview", financeOverviewMonth:"2026-07",
  canViewFinanceCosts:true,
  financeOverviewState:{{loading:false,error:"",loaded:false}},
  financeOverview:{{trend:[{{owner:"A-before"}}]}},
  financePayableState:{{loading:false,error:""}},
  financePayables:[{{owner:"A-before"}}], financePayableSuppliers:[{{owner:"A-before"}}],
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
    return error?.code === "ERR_CANCELED" || error?.name === "CanceledError" || error?.name === "AbortError";
  }},
  errorMessage(error) {{ return error?.message || String(error); }},
}};
vm.loadFinance = new AsyncFunction({json.dumps(load_finance_body, ensure_ascii=False)}).bind(vm);
vm.loadFinanceOverview = new AsyncFunction("sessionContext", {json.dumps(overview_body, ensure_ascii=False)}).bind(vm);
vm.loadFinancePayables = new AsyncFunction("sessionContext", {json.dumps(payables_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const actorAOverview = vm.loadFinance();
  await Promise.resolve();
  const actorARequests = pending.splice(0, 3);
  const actorARequest = actorARequests.find(item => item.url === "/api/finance/overview");
  const actorAWorkbench = actorARequests.find(item => item.url.includes("current-customer-months"));
  const actorACost = actorARequests.find(item => item.url.includes("cost-pool/summary"));
  if (!actorARequest || actorARequest.config.params.through_month !== "2026-07") throw new Error("overview did not freeze its month");
  if (!actorAWorkbench || actorAWorkbench.config.params.statement_month !== "2026-07") throw new Error("workbench did not freeze its month");
  if (!actorACost || actorACost.config.params.month !== "2026-07") throw new Error("cost summary did not freeze its month");

  vm.authGeneration = 8;
  vm.user = {{id:202}};
  vm.financeOverview = {{trend:[{{owner:"B"}}]}};
  vm.financeOverviewState = {{loading:true,error:"B-loading",loaded:true}};
  actorARequest.resolve({{data:{{trend:[{{owner:"A-late"}}]}}}});
  actorAWorkbench.resolve({{data:{{summary:{{pending_reconciliation_amount:999}}}}}});
  actorACost.resolve({{data:{{status_totals:{{draft:{{amount:999}}}}}}}});
  if (await actorAOverview !== false) throw new Error("old actor overview reported success");
  if (vm.financeOverview.trend[0].owner !== "B") throw new Error("old actor overview replaced new actor data");
  if (!vm.financeOverviewState.loading || vm.financeOverviewState.error !== "B-loading") throw new Error("old actor overview finally replaced new actor state");

  vm.financeOverviewMonth = "2026-08";
  vm.financeOverviewState = {{loading:false,error:"",loaded:false}};
  const olderOverview = vm.loadFinanceOverview();
  await Promise.resolve();
  const olderOverviewRequests = pending.splice(0, 3);
  const olderOverviewRequest = olderOverviewRequests.find(item => item.url === "/api/finance/overview");
  vm.financeOverviewMonth = "2026-09";
  const newerOverview = vm.loadFinanceOverview();
  await Promise.resolve();
  const newerOverviewRequests = pending.splice(0, 3);
  const newerOverviewRequest = newerOverviewRequests.find(item => item.url === "/api/finance/overview");
  if (olderOverviewRequest.config.params.through_month !== "2026-08" || newerOverviewRequest.config.params.through_month !== "2026-09") throw new Error("overview request months were not isolated");
  olderOverviewRequest.resolve({{data:{{trend:[{{owner:"older"}}]}}}});
  olderOverviewRequests.find(item => item.url.includes("current-customer-months")).resolve({{data:{{summary:{{pending_reconciliation_amount:888}}}}}});
  olderOverviewRequests.find(item => item.url.includes("cost-pool/summary")).resolve({{data:{{status_totals:{{draft:{{amount:888}}}}}}}});
  if (await olderOverview !== false) throw new Error("older overview reported success");
  if (!vm.financeOverviewState.loading || vm.financeOverview.trend[0].owner !== "B") throw new Error("older overview response or finally replaced the active request state");
  newerOverviewRequest.resolve({{data:{{trend:[{{owner:"newer"}}]}}}});
  newerOverviewRequests.find(item => item.url.includes("current-customer-months")).resolve({{data:{{summary:{{pending_reconciliation_amount:2544}}}}}});
  newerOverviewRequests.find(item => item.url.includes("cost-pool/summary")).resolve({{data:{{status_totals:{{draft:{{amount:0}}}}}}}});
  if (await newerOverview !== true || vm.financeOverviewState.loading || vm.financeOverview.trend[0].owner !== "newer") throw new Error("newer overview was not committed and unlocked");
  if (Number(vm.financeOverview.workbench_summary.pending_reconciliation_amount) !== 2544) throw new Error("newer workbench summary was not committed");

  vm.financeOverviewMonth = "2026-10";
  const failedOverview = vm.loadFinanceOverview();
  await Promise.resolve();
  const failedOverviewRequests = pending.splice(0, 3);
  failedOverviewRequests.find(item => item.url === "/api/finance/overview").resolve({{data:{{trend:[{{owner:"must-not-commit"}}]}}}});
  failedOverviewRequests.find(item => item.url.includes("current-customer-months")).resolve({{data:{{summary:{{pending_reconciliation_amount:777}}}}}});
  failedOverviewRequests.find(item => item.url.includes("cost-pool/summary")).reject(new Error("成本摘要读取失败"));
  if (await failedOverview !== false || vm.financeOverviewState.loading) throw new Error("failed cost summary did not fail closed");
  if (!vm.financeOverviewState.error.includes("成本摘要读取失败")) throw new Error("failed cost summary was hidden");
  if (vm.financeOverview.trend[0].owner !== "newer" || Number(vm.financeOverview.workbench_summary.pending_reconciliation_amount) !== 2544) throw new Error("partial overview data was committed after cost failure");

  vm.canViewFinanceCosts = false;
  vm.financeView = "payables";
  const olderPayables = vm.loadFinance();
  await Promise.resolve();
  const olderPayablesRows = pending.shift();
  const olderPayablesSuppliers = pending.shift();
  const newerPayables = vm.loadFinancePayables();
  await Promise.resolve();
  const newerPayablesRows = pending.shift();
  const newerPayablesSuppliers = pending.shift();
  olderPayablesRows.reject(new Error("older payables failed"));
  olderPayablesSuppliers.resolve({{data:{{items:[{{owner:"older"}}]}}}});
  if (await olderPayables !== false) throw new Error("older payables reported success");
  if (!vm.financePayableState.loading || vm.financePayableState.error) throw new Error("stale payables catch or finally replaced active request state");
  newerPayablesRows.resolve({{data:{{items:[{{owner:"newer"}}]}}}});
  newerPayablesSuppliers.resolve({{data:{{items:[{{owner:"newer-supplier"}}]}}}});
  if (await newerPayables !== true || vm.financePayableState.loading || vm.financePayableState.error) throw new Error("newer payables did not finish cleanly");
  if (vm.financePayables[0].owner !== "newer" || vm.financePayableSuppliers[0].owner !== "newer-supplier") throw new Error("newer payables data was not committed");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "finance-overview-payables-latest-request.js"
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

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the login startup regression"
    target = tmp_path / "login-supplier-parallel.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_session_and_login_share_parallel_initial_loader() -> None:
    check_session = _method_body("async checkSession() {", "async login() {")
    login = _method_body("async login() {", "async logout() {")
    initial = _method_body(
        "async loadInitialPageResources() {", "async loadPage(page, { force=false, supplierPromise=null } = {}) {"
    )

    assert check_session.count("if (!await this.loadInitialPageResources()) return;") == 1
    assert "const initialResourcesReady = await this.loadInitialPageResources();" in login
    assert "if (!requestIsCurrent() || !initialResourcesReady) return;" in login
    for block in (check_session, login):
        assert "await this.loadSuppliers();" not in block
        assert "await this.loadPage(this.activePage, { force:true });" not in block
    assert "const supplierPromise = this.loadSuppliers();" in initial
    assert "const requestAuthGeneration = this.authGeneration;" in initial
    assert "const requestUserId = this.user?.id ?? null;" in initial
    assert "this.loadPage(this.activePage" in initial
    assert "supplierPromise," in initial
    assert "await Promise.all([supplierPromise, pagePromise]);" in initial
    assert check_session.index("/api/auth/me") < check_session.index(
        "loadInitialPageResources()"
    )
    assert login.index("/api/auth/me") < login.index(
        "loadInitialPageResources()"
    )
    reset = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")
    assert "this.pageLoadSequence += 1;" in reset
    assert "this.loginAttemptSequence += 1;" in reset
    assert "this.loading = false;" in reset
    assert "this.deliveryDetailRequestSequence += 1;" in reset
    assert "this.deliveryDetailState = {};" in reset
    assert "this.expandedDeliveryRows = {};" in reset
    assert "const requestSequence = ++this.loginAttemptSequence;" in login
    assert "if (requestIsCurrent()) this.loading = false;" in login
    assert "if (!requestIsCurrent()) return;" in check_session
    assert "if (!requestIsCurrent()) return;" in login
    assert "const loginPayload = {...this.loginForm};" in login

    suppliers = _method_body(
        "async loadSuppliers(includeInactive = this.canAdmin) {",
        "defaultSupplierName() {",
    )
    assert 'axios.get("/api/master/suppliers", {params:{include_inactive:true}' in suppliers
    assert 'axios.get("/api/master/suppliers/candidates"' in suppliers


def test_initial_resources_start_together_wait_for_both_and_reuse_supplier_request(
    tmp_path: Path,
) -> None:
    initial_body = _method_body(
        "async loadInitialPageResources() {", "async loadPage(page, { force=false, supplierPromise=null } = {}) {"
    )
    page_body = _method_body(
        "async loadPage(page, { force=false, supplierPromise=null } = {}) {",
        "refreshCurrent() {",
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const deferred = () => {{
  let resolve;
  const promise = new Promise(done => {{ resolve = done; }});
  return {{promise, resolve}};
}};
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
const initialBody = {json.dumps(initial_body, ensure_ascii=False)};
const pageBody = {json.dumps(page_body, ensure_ascii=False)};

(async () => {{
  const supplier = deferred();
  const page = deferred();
  const starts = [];
  let settled = false;
  let startupResult = null;
  const vm = {{
    activePage: "dashboard",
    authGeneration: 0,
    user: {{id: 1}},
    loadSuppliers() {{ starts.push("supplier"); return supplier.promise; }},
    loadPage(pageName, options) {{
      starts.push("page");
      expect(pageName === "dashboard", "wrong initial page");
      expect(options.force === true, "initial page was not forced");
      expect(options.supplierPromise === supplier.promise, "supplier promise was not shared");
      return page.promise;
    }},
  }};
  vm.loadInitialPageResources = new AsyncFunction(initialBody).bind(vm);
  const startup = vm.loadInitialPageResources().then(result => {{ settled = true; startupResult = result; }});
  expect(starts.join(",") === "supplier,page", "supplier and page did not start together");
  page.resolve("ready");
  await Promise.resolve();
  expect(!settled, "startup did not wait for supplier result");
  supplier.resolve(false);
  await startup;
  expect(settled, "supplier false blocked the successful page");
  expect(startupResult === true, "current session startup did not report completion");

  const staleSupplier = deferred();
  const stalePage = deferred();
  const staleVm = {{
    activePage: "dashboard", authGeneration: 4, user: {{id: 41}},
    loadSuppliers() {{ return staleSupplier.promise; }},
    loadPage() {{ return stalePage.promise; }},
  }};
  staleVm.loadInitialPageResources = new AsyncFunction(initialBody).bind(staleVm);
  const staleStartup = staleVm.loadInitialPageResources();
  staleVm.authGeneration = 5;
  staleVm.user = {{id: 52}};
  stalePage.resolve(true);
  staleSupplier.resolve(true);
  expect(await staleStartup === false, "replaced session startup was accepted as current");

  const sharedSupplier = deferred();
  let supplierCalls = 0;
  const supplierPageVm = {{
    activePage: "products", productTab: "suppliers", canAdmin: true,
    loading: false, selectedProductCustomer: null,
    pageLoadSequence: 0, authGeneration: 0, user: {{id: 1}},
    loadSuppliers() {{ supplierCalls += 1; return sharedSupplier.promise; }},
    pageCacheFresh() {{ return false; }},
    invalidatePageCache() {{}},
    loadCustomerOptions: async () => true,
    markPageCache(pageName) {{ this.cachedPage = pageName; }},
    showToast(message) {{ throw new Error(`unexpected page failure: ${{message}}`); }},
  }};
  supplierPageVm.loadPage = new AsyncFunction(
    "page", "{{force=false,supplierPromise=null}}={{}}", pageBody
  ).bind(supplierPageVm);
  supplierPageVm.loadInitialPageResources = new AsyncFunction(initialBody).bind(supplierPageVm);
  const supplierPageStartup = supplierPageVm.loadInitialPageResources();
  await Promise.resolve();
  await Promise.resolve();
  expect(supplierCalls === 1, "products/suppliers issued a duplicate supplier request");
  sharedSupplier.resolve(true);
  await supplierPageStartup;
  expect(supplierCalls === 1, "shared supplier promise was not reused");
  expect(supplierPageVm.cachedPage === "products", "supplier page did not finish normally");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)


def test_replaced_session_cannot_open_old_password_change_flow(tmp_path: Path) -> None:
    check_session_body = _method_body("async checkSession() {", "async login() {")
    login_body = _method_body("async login() {", "async logout() {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const deferred = () => {{
  let resolve;
  const promise = new Promise(done => {{ resolve = done; }});
  return {{promise, resolve}};
}};
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
globalThis.localStorage = {{setItem() {{}}, removeItem() {{}}}};

(async () => {{
  let startupGate = deferred();
  let passwordPrompts = [];
  globalThis.axios = {{
    get: async () => ({{data:{{user:{{id:1,must_change_password:true}},permissions:[],customer_scope:[],unrestricted_customer_access:true}}}}),
    post: async () => ({{data:{{ok:true}}}}),
  }};
  const common = {{
    authGeneration: 0, loginAttemptSequence: 0, user: null, loginError: "", loading: false,
    loadProductBoxTypeRules: async () => true,
    loadEffectiveUiLayout: async () => true,
    redirectAfterLogin: () => false,
    initialPageFromLocation: () => "dashboard",
    pageAllowed: () => true,
    firstAllowedPage: () => "dashboard",
    loadInitialPageResources: () => startupGate.promise,
    openChangePassword: (...args) => passwordPrompts.push(args),
    errorMessage: error => String(error?.message || error),
  }};

  const sessionVm = {{...common}};
  sessionVm.checkSession = new AsyncFunction({json.dumps(check_session_body, ensure_ascii=False)}).bind(sessionVm);
  const oldSession = sessionVm.checkSession();
  await Promise.resolve(); await Promise.resolve();
  sessionVm.authGeneration += 1;
  sessionVm.user = null;
  sessionVm.loginError = "登录已失效，请重新登录";
  startupGate.resolve(false);
  await oldSession;
  expect(passwordPrompts.length === 0, "logged-out session opened password change");
  expect(sessionVm.loginError === "登录已失效，请重新登录", "old session replaced the auth-expired message");

  startupGate = deferred();
  passwordPrompts = [];
  const loginVm = {{
    ...common,
    loginForm: {{remember_me:false,username:"old-user",password:"old-password"}},
    loadInitialPageResources: () => startupGate.promise,
    openChangePassword: (...args) => passwordPrompts.push(args),
  }};
  loginVm.login = new AsyncFunction({json.dumps(login_body, ensure_ascii=False)}).bind(loginVm);
  const oldLogin = loginVm.login();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  loginVm.authGeneration += 1;
  loginVm.user = {{id:2,must_change_password:true}};
  startupGate.resolve(false);
  await oldLogin;
  expect(passwordPrompts.length === 0, "old password was offered to a replacement account");

  const firstStartup = deferred();
  const secondStartup = deferred();
  passwordPrompts = [];
  let startupCalls = 0;
  let authReads = 0;
  globalThis.axios = {{
    post: async () => ({{data:{{ok:true}}}}),
    get: async () => {{
      authReads += 1;
      return {{data:{{
        user:{{id:7,must_change_password:true}},
        permissions:[],customer_scope:[],unrestricted_customer_access:true,
      }}}};
    }},
  }};
  const concurrentVm = {{
    ...common,
    loginAttemptSequence: 0,
    loginForm: {{remember_me:false,username:"old-user",password:"old-password"}},
    loadInitialPageResources() {{
      startupCalls += 1;
      return startupCalls === 1 ? firstStartup.promise : secondStartup.promise;
    }},
    openChangePassword: (...args) => passwordPrompts.push(args),
  }};
  concurrentVm.login = new AsyncFunction({json.dumps(login_body, ensure_ascii=False)}).bind(concurrentVm);
  const firstLogin = concurrentVm.login();
  for (let index = 0; index < 12 && startupCalls < 1; index += 1) await Promise.resolve();
  expect(startupCalls === 1, "first login did not reach initial page loading");
  concurrentVm.loginForm.username = "new-user";
  concurrentVm.loginForm.password = "new-password";
  const secondLogin = concurrentVm.login();
  for (let index = 0; index < 12 && startupCalls < 2; index += 1) await Promise.resolve();
  expect(startupCalls === 2, "replacement login did not reach initial page loading");
  expect(concurrentVm.loading === true, "replacement login did not own loading state");
  firstStartup.resolve(true);
  await firstLogin;
  expect(concurrentVm.loading === true, "old login cleared replacement login loading state");
  expect(passwordPrompts.length === 0, "old login opened its password prompt during replacement");
  secondStartup.resolve(true);
  await secondLogin;
  expect(concurrentVm.loading === false, "current login did not release loading state");
  expect(passwordPrompts.length === 1, "current account did not open exactly one password-change prompt");
  expect(passwordPrompts[0][1] === "new-password", "password-change prompt received stale login credentials");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)


def test_stale_page_result_cannot_write_cache_or_finish_new_session_loading(
    tmp_path: Path,
) -> None:
    page_body = _method_body(
        "async loadPage(page, { force=false, supplierPromise=null } = {}) {",
        "refreshCurrent() {",
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const deferred = () => {{
  let resolve;
  const promise = new Promise(done => {{ resolve = done; }});
  return {{promise, resolve}};
}};
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
const pageBody = {json.dumps(page_body, ensure_ascii=False)};

(async () => {{
  const oldPage = deferred();
  const newPage = deferred();
  let calls = 0;
  const cacheWrites = [];
  const vm = {{
    pageLoadSequence: 0,
    authGeneration: 0,
    user: {{id: 1}},
    loading: false,
    pageCacheFresh() {{ return false; }},
    invalidatePageCache() {{}},
    loadOverview() {{ calls += 1; return calls === 1 ? oldPage.promise : newPage.promise; }},
    markPageCache(page) {{ cacheWrites.push(`${{this.user.id}}:${{page}}`); }},
    showToast(message) {{ throw new Error(`unexpected toast: ${{message}}`); }},
    errorMessage(error) {{ return String(error?.message || error); }},
  }};
  vm.loadPage = new AsyncFunction(
    "page", "{{force=false,supplierPromise=null}}={{}}", pageBody
  ).bind(vm);

  const oldRequest = vm.loadPage("dashboard", {{force: true}});
  await Promise.resolve();
  expect(vm.loading === true, "old page did not enter loading state");

  vm.authGeneration += 1;
  vm.pageLoadSequence += 1;
  vm.user = {{id: 2}};
  const newRequest = vm.loadPage("dashboard", {{force: true}});
  await Promise.resolve();
  oldPage.resolve(true);
  await oldRequest;
  expect(vm.loading === true, "stale page cleared the new session loading state");
  expect(cacheWrites.length === 0, "stale page restored an old page-cache timestamp");

  newPage.resolve(true);
  await newRequest;
  expect(vm.loading === false, "current page did not finish loading");
  expect(cacheWrites.join(",") === "2:dashboard", "current page cache was not written once");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)


def test_auth_me_response_cannot_revive_logged_out_session(tmp_path: Path) -> None:
    check_session_body = _method_body("async checkSession() {", "async login() {")
    login_body = _method_body("async login() {", "async logout() {")
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const deferred = () => {{
  let resolve;
  const promise = new Promise(done => {{ resolve = done; }});
  return {{promise, resolve}};
}};
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
globalThis.localStorage = {{setItem() {{}}, removeItem() {{}}}};
const authData = userId => ({{data:{{
  user:{{id:userId,must_change_password:true}},
  permissions:[],customer_scope:[],unrestricted_customer_access:true,
}}}});

(async () => {{
  const sessionMe = deferred();
  let sessionResources = 0;
  let sessionRules = 0;
  let passwordPrompts = 0;
  globalThis.axios = {{get: () => sessionMe.promise}};
  const sessionVm = {{
    authGeneration:0,user:null,loginError:"",activePage:"dashboard",
    loadProductBoxTypeRules:async () => {{ sessionRules += 1; }},
    loadEffectiveUiLayout:async () => true,
    redirectAfterLogin:() => false,initialPageFromLocation:() => "dashboard",
    pageAllowed:() => true,firstAllowedPage:() => "dashboard",
    loadInitialPageResources:async () => {{ sessionResources += 1; return true; }},
    openChangePassword:() => {{ passwordPrompts += 1; }},
    errorMessage:error => String(error?.message || error),
  }};
  sessionVm.checkSession = new AsyncFunction({json.dumps(check_session_body, ensure_ascii=False)}).bind(sessionVm);
  const oldSession = sessionVm.checkSession();
  await Promise.resolve();
  sessionVm.authGeneration += 1;
  sessionVm.user = null;
  sessionVm.loginError = "登录已失效，请重新登录";
  sessionMe.resolve(authData(11));
  await oldSession;
  expect(sessionVm.user === null, "stale auth/me revived a logged-out session");
  expect(sessionRules === 0 && sessionResources === 0, "stale session started protected resources");
  expect(passwordPrompts === 0, "stale session opened password change");
  expect(sessionVm.loginError === "登录已失效，请重新登录", "stale session replaced auth-expired message");

  const loginMe = deferred();
  let loginResources = 0;
  let loginRules = 0;
  passwordPrompts = 0;
  globalThis.axios = {{
    post:async () => ({{data:{{ok:true}}}}),
    get:() => loginMe.promise,
  }};
  const loginVm = {{
    authGeneration:0,loginAttemptSequence:0,user:null,loginError:"",loading:false,
    activePage:"dashboard",loginForm:{{remember_me:false,username:"old",password:"secret"}},
    loadProductBoxTypeRules:async () => {{ loginRules += 1; }},
    loadEffectiveUiLayout:async () => true,
    redirectAfterLogin:() => false,initialPageFromLocation:() => "dashboard",
    pageAllowed:() => true,firstAllowedPage:() => "dashboard",
    loadInitialPageResources:async () => {{ loginResources += 1; return true; }},
    openChangePassword:() => {{ passwordPrompts += 1; }},
    errorMessage:error => String(error?.message || error),
  }};
  loginVm.login = new AsyncFunction({json.dumps(login_body, ensure_ascii=False)}).bind(loginVm);
  const oldLogin = loginVm.login();
  await Promise.resolve(); await Promise.resolve();
  loginVm.authGeneration += 1;
  loginVm.loginAttemptSequence += 1;
  loginVm.user = null;
  loginVm.loginError = "登录已失效，请重新登录";
  loginMe.resolve(authData(12));
  await oldLogin;
  expect(loginVm.user === null, "stale login auth/me revived a logged-out session");
  expect(loginRules === 0 && loginResources === 0, "stale login started protected resources");
  expect(passwordPrompts === 0, "stale login opened password change");
  expect(loginVm.loading === true, "stale login cleared replacement loading state");
  expect(loginVm.loginError === "登录已失效，请重新登录", "stale login replaced auth-expired message");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*?)</script>", INCOMING, re.DOTALL)
    assert match is not None
    return match.group(1)


def _between(start: str, end: str) -> str:
    source = _script()
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript behavior validation"
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


def test_standalone_pending_uses_server_total_and_current_page_contract() -> None:
    load_pending = _between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function loadReceived",
    )
    refresh = _between(
        "async function refreshAfterIncomingWrite()",
        "function findItem",
    )
    revert = _between(
        '("revertForm").addEventListener("submit"',
        "async function retryCurrentView",
    )

    for marker in (
        'id="pendingPager"',
        'id="pendingPreviousPage"',
        'id="pendingNextPage"',
        "pendingTotal: 0",
        "pendingPage: 1",
        "pendingRetryPage: 1",
        "pendingPageSize: 20",
        "state.pendingTotal",
    ):
        assert marker in INCOMING
    assert 'endpoint = "/api/incoming/pending"' in load_pending
    assert 'params.set("dimension_mode", requestedDimensionMode)' in load_pending
    assert "pending.total" in load_pending
    assert "pending.page" in load_pending
    assert "pending.page_size" in load_pending
    assert "loadPending({page: state.pendingPage})" in refresh
    assert "await refreshAfterIncomingWrite();" in revert


def test_standalone_pending_latest_failure_and_server_clamp_keep_last_good(
    tmp_path: Path,
) -> None:
    helpers = _between("function beginLatestRequest(key)", "function createIdempotencyKey()")
    pending_methods = _between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function loadReceived",
    )
    retry = _between(
        "async function retryCurrentView()",
        '$("refreshButton").addEventListener',
    )
    harness = f"""
class FakeAbortController {{
  constructor() {{ this.signal = {{aborted:false}}; }}
  abort() {{ this.signal.aborted = true; }}
}}
global.AbortController = FakeAbortController;
const requestControllers = new Map();
const nodes = {{
  loadingState: {{hidden:true}}, errorState: {{hidden:true}}, errorMessage: {{textContent:""}},
  authNotice: {{hidden:true}}
}};
const $ = id => nodes[id];
const state = {{
  activeTab:"pending", pending:[{{item_id:"r1", incoming_quantity:7}}], pendingTotal:51, pendingPage:2,
  pendingRetryPage:2, pendingPageSize:25, pendingLoading:false, pendingError:"", expandedIds:new Set(["r1"]),
  authGeneration:1, user:{{id:7}}, sessionIdentity:{{user_id:7,auth_version:1}}
}};
const calls = [];
function api(url, options) {{
  return new Promise((resolve, reject) => calls.push({{url, resolve, reject, signal:options.signal}}));
}}
function render() {{}}
function ensureReceiptLocations() {{}}
function toChineseMessage(error) {{ return String(error?.message || error); }}
async function checkAuth() {{ return true; }}
async function loadData() {{ throw new Error("error retry must not use the last-good page"); }}
{helpers}
{pending_methods}
{retry}
(async () => {{
  const oldRequest = loadPending({{page:2}});
  const failingRequest = loadPending({{page:3}});
  if (!calls[1].url.includes("page=3&page_size=25")) throw new Error("paged URL missing");
  calls[0].resolve({{items:[{{item_id:"old"}}], total:1, page:1, page_size:25, session_identity_header:"7:1"}});
  await oldRequest;
  if (state.pending[0]?.item_id !== "r1" || state.pendingPage !== 2) throw new Error("old response replaced last-good page");
  calls[1].reject(new Error("network down"));
  const failed = await failingRequest;
  if (failed !== false) throw new Error("failed page request reported success");
  if (state.pending[0]?.item_id !== "r1" || state.pending[0]?.incoming_quantity !== 7) throw new Error("failure cleared last-good draft");
  if (state.pendingTotal !== 51 || state.pendingPage !== 2) throw new Error("failure changed applied pagination");
  if (state.pendingRetryPage !== 3) throw new Error("failed target page was not retained for retry");
  if (!state.pendingError.includes("network down")) throw new Error("failure message missing");

  const clampedRequest = retryCurrentView();
  if (!calls[2].url.includes("page=3&page_size=25")) throw new Error("retry did not request the failed page");
  calls[2].resolve({{items:[{{item_id:"sr9", remaining_quantity:4}}], total:10, page:1, page_size:25, session_identity_header:"7:1"}});
  const succeeded = await clampedRequest;
  if (succeeded !== true) throw new Error("clamped response failed");
  if (state.pendingPage !== 1 || state.pendingTotal !== 10) throw new Error("server clamp was not applied");
  if (state.pendingRetryPage !== 1) throw new Error("successful clamp did not reset retry page");
  if (state.pending[0]?.item_id !== "sr9") throw new Error("string route identity was not retained");
  if (state.expandedIds.size !== 0) throw new Error("successful page change kept old expansion");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(harness, tmp_path, "p1-36l-incoming-standalone-pagination.js")


def test_standalone_pending_ignores_response_from_old_auth_generation(
    tmp_path: Path,
) -> None:
    helpers = _between("function beginLatestRequest(key)", "function createIdempotencyKey()")
    load_pending = _between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function changePendingPage",
    )
    harness = f"""
class FakeAbortController {{
  constructor() {{ this.signal = {{aborted:false}}; }}
  abort() {{ this.signal.aborted = true; }}
}}
global.AbortController = FakeAbortController;
const requestControllers = new Map();
const nodes = {{loadingState:{{hidden:true}},errorState:{{hidden:true}},errorMessage:{{textContent:""}},authNotice:{{hidden:true}}}};
const $ = id => nodes[id];
const state = {{
  pending:[{{item_id:"r1"}}], pendingTotal:1, pendingPage:1, pendingPageSize:25,
  pendingRetryPage:1, pendingLoading:false, pendingError:"", expandedIds:new Set(), authGeneration:4, user:{{id:7}}, sessionIdentity:{{user_id:7,auth_version:1}}
}};
let resolveRequest;
function api() {{ return new Promise(resolve => {{ resolveRequest = resolve; }}); }}
function render() {{}}
function ensureReceiptLocations() {{}}
function toChineseMessage(error) {{ return String(error?.message || error); }}
{helpers}
{load_pending}
(async () => {{
  const request = loadPending({{page:1}});
  state.authGeneration = 5;
  state.user = {{id:8}};
  resolveRequest({{items:[{{item_id:"foreign"}}], total:1, page:1, page_size:25, session_identity_header:"7:1"}});
  const result = await request;
  if (result !== false) throw new Error("old account response reported success");
  if (state.pending[0]?.item_id !== "r1") throw new Error("old account response entered new session");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(harness, tmp_path, "p1-36l-incoming-standalone-auth.js")


def test_standalone_account_switch_clears_old_rows_before_new_failure(
    tmp_path: Path,
) -> None:
    reset = _between(
        "function resetAuthenticatedIncomingState(",
        "function beginLatestRequest(key)",
    )
    request_helpers = _between(
        "function beginLatestRequest(key)",
        "function createIdempotencyKey()",
    )
    check_auth = _between("async function checkAuth()", "function formatDate")
    load_pending = _between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function changePendingPage",
    )
    harness = f"""
class FakeAbortController {{
  constructor() {{ this.signal = {{aborted:false}}; }}
  abort() {{ this.signal.aborted = true; }}
}}
global.AbortController = FakeAbortController;
const requestControllers=new Map();
const nodes={{
  pendingCount:{{textContent:"9"}},receivedCount:{{textContent:"2"}},cardList:{{innerHTML:"secret"}},
  pendingPager:{{hidden:false}},emptyState:{{hidden:false}},loadingState:{{hidden:true}},
  errorState:{{hidden:true}},errorMessage:{{textContent:""}},authNotice:{{hidden:true,textContent:""}},
  userLabel:{{textContent:""}}
}};
const $=id=>nodes[id];
const state={{
  activeTab:"pending",pending:[{{item_id:"r-secret",customer_name:"客户A"}}],pendingTotal:9,
  pendingPage:2,pendingRetryPage:2,pendingPageSize:25,pendingLoading:false,pendingError:"",
  received:[{{customer_name:"客户A"}}],receivedLoaded:true,receivedLoading:false,receivedError:"",
  locations:[{{id:1}}],locationsLoading:false,receiptLocations:[{{id:2}}],receiptLocationsLoading:false,
  user:{{id:1,role:"sales"}},permissions:["incoming.view"],revertingItemId:null,
  busyItemIds:new Set(),receiveIdempotencyKeys:new Map(),revertSubmitting:false,expandedIds:new Set(["r-secret"]),
  authGeneration:3,
}};
const hasPermission=code=>state.user?.role==="admin"||state.permissions.includes(code);
let phase="auth";
async function api(url){{
  if(phase==="auth"){{phase="pending";return {{user:{{id:2,real_name:"账号B",role:"sales"}},permissions:["incoming.view"]}};}}
  throw new Error("new account network failed");
}}
function render(){{}}
function ensureReceiptLocations(){{}}
function toChineseMessage(error){{return String(error?.message||error);}}
{reset}
{request_helpers}
{check_auth}
{load_pending}
(async()=>{{
  if(await checkAuth()!==true)throw new Error("new account auth failed");
  if(state.user?.id!==2)throw new Error("new account was not applied");
  if(state.pending.length||state.pendingTotal||state.received.length)throw new Error("old account rows survived identity switch");
  if(await loadPending({{page:1}})!==false)throw new Error("new account failed request reported success");
  if(state.pending.length||state.pendingTotal)throw new Error("new account failure exposed old rows");
  if(!state.pendingError.includes("new account network failed"))throw new Error("new account error missing");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(harness, tmp_path, "p1-36l-incoming-standalone-account-switch.js")


def test_standalone_incoming_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    target = tmp_path / "incoming-p1-36l.js"
    target.write_text(_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

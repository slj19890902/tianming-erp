from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")
VERSION = (ROOT / "app" / "version.py").read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_mobile_incoming_uses_one_vertical_scroll_owner() -> None:
    body_rule = _block(MOBILE, "body.incoming-frame-active {", "body.incoming-frame-active .app")
    app_rule = _block(MOBILE, "body.incoming-frame-active .app {", "body.incoming-frame-active main")
    page_rule = _block(MOBILE, "body.incoming-frame-active #incomingPage {", "body.incoming-frame-active #incomingFrame")
    frame_rule = _block(MOBILE, "body.incoming-frame-active #incomingFrame {", ".searchbar")
    assert "height: 100dvh" in body_rule and "overflow: hidden" in body_rule
    assert "height: 100dvh" in app_rule and "min-height: 0" in app_rule
    assert "grid-template-rows: auto minmax(0, 1fr)" in app_rule
    assert "height: 100%" in page_rule and "min-height: 0" in page_rule
    assert "height: 100%" in frame_rule and "min-height: 0" in frame_rule
    assert 'document.body.classList.toggle("incoming-frame-active", isIncoming);' in MOBILE
    assert 'const isIncoming = page === "incoming";' in MOBILE
    assert "incoming-frame-active #preDelivery" not in MOBILE
    assert "const outerScrollTop = isIncoming ? 0 : (state.scrollByPage.get(page) || 0);" in MOBILE
    assert "env(safe-area-inset-bottom)" in MOBILE
    assert ".bottom-nav {" in MOBILE and "position: fixed" in MOBILE


def test_mobile_incoming_frame_remains_lazy_and_preserves_child_state() -> None:
    assert 'id="incomingFrame" class="embedded-entry"' in MOBILE
    assert 'src="about:blank"' in MOBILE
    set_page = _block(MOBILE, "function setPage(page", "function showStatus")
    assert 'page === "incoming" && !state.loadedFrames.has(page)' in set_page
    assert 'byId("incomingFrame").src = "/incoming.html?embedded=1";' in set_page
    assert "state.loadedFrames.add(page);" in set_page
    assert 'byId("incomingFrame").src = "about:blank"' not in set_page
    assert 'type: "tm-mobile-incoming-performance-resume"' in set_page
    assert 'event.data?.type !== "tm-mobile-incoming-performance-resume"' in INCOMING
    assert 'requests: []' in INCOMING


def test_mobile_incoming_has_cross_document_phase_measurements() -> None:
    for stage in (
        "entry",
        "shell-ready",
        "document-ready",
        "auth-ready",
        "data-ready",
        "content-ready",
    ):
        assert f'tm.mobile.incoming.{stage}' in MOBILE or f'"{stage}"' in MOBILE
    for measure in (
        "entry-to-shell",
        "shell-to-document",
        "document-to-auth",
        "document-to-data",
        "data-to-content",
        "entry-to-content",
    ):
        assert measure in MOBILE
    assert 'event.origin !== window.location.origin' in MOBILE
    assert 'event.source !== byId("incomingFrame").contentWindow' in MOBILE
    assert 'type: "tm-mobile-incoming-performance"' in INCOMING
    assert "__tmMobileIncomingPerformanceSnapshot" in MOBILE


def test_initial_auth_and_pending_read_are_parallel_but_data_is_gated() -> None:
    init = _block(INCOMING, "async function init()", "init();")
    pending = _block(
        INCOMING,
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function changePendingPage",
    )
    assert "const authorizationPromise = checkAuth();" in init
    assert "authorizationPromise" in init
    assert "const pendingPromise = api(" in pending
    assert "Promise.allSettled([authorizationPromise, pendingPromise])" in pending
    assert "authorizationResult.value !== true" in pending
    assert pending.index("Promise.allSettled") < pending.index("state.pending = rows.map")
    assert "authGeneration = Number(state.authGeneration || 0);" in pending
    assert "userId = String(state.user?.id ?? \"\");" in pending
    assert 'api("/api/auth/me"' in INCOMING
    assert 'hasPermission("incoming.view")' in INCOMING
    assert "pending.session_identity_header" in pending
    assert "authIdentity?.user_id" in pending
    assert "expectedIdentity" in pending
    assert "authIdentity?.auth_version" in pending
    assert "X-ERP-Session-Identity" in INCOMING


def test_performance_diagnostics_are_sanitized_and_mark_first_content() -> None:
    assert 'response.headers.get("Server-Timing")' in INCOMING
    assert 'response.headers.get("X-Request-ID")' in INCOMING
    assert "endpoint_alias" in INCOMING
    assert "status" in INCOMING
    assert "duration_ms" in INCOMING
    assert "request_url" not in INCOMING
    assert "request_body" not in INCOMING
    assert "reportIncomingContentReady" in INCOMING
    assert "window.requestAnimationFrame" in INCOMING


def test_current_mobile_incoming_release_note_covers_identity_fix() -> None:
    assert "手机待收料身份响应修复" in VERSION
    assert "待收料接口" in VERSION
    assert "HTTP响应头" in VERSION
    assert "登录版本" in VERSION
    assert "无数据库迁移" in VERSION


def test_initial_pending_never_applies_before_authorization(tmp_path: Path) -> None:
    script_match = re.search(r"<script>(.*?)</script>", INCOMING, re.DOTALL)
    assert script_match is not None
    script = script_match.group(1)

    def between(start: str, end: str) -> str:
        begin = script.index(start)
        return script[begin : script.index(end, begin)]

    helpers = between("function beginLatestRequest(key)", "function createIdempotencyKey()")
    pending = between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function changePendingPage",
    )
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript behavior validation"
    target = tmp_path / "candidate-c-auth-gated-pending.js"
    target.write_text(
        f"""
class FakeAbortController {{
  constructor() {{ this.signal = {{aborted:false}}; }}
  abort() {{ this.signal.aborted = true; }}
}}
global.AbortController = FakeAbortController;
const requestControllers = new Map();
const nodes = {{loadingState:{{hidden:true}},errorState:{{hidden:true}},errorMessage:{{textContent:""}},authNotice:{{hidden:true}}}};
const $ = id => nodes[id];
const state = {{pending:[],pendingTotal:0,pendingPage:1,pendingPageSize:20,pendingRetryPage:1,pendingLoading:false,pendingError:"",expandedIds:new Set(),authGeneration:0,user:null,permissions:[],sessionIdentity:null,initialContentReported:false,performanceRequests:[],received:[],receivedLoaded:false,receivedLoading:false,receivedError:"",locations:[],locationsLoading:false,busyItemIds:new Set(),receiveIdempotencyKeys:new Map(),revertingItemId:null,revertSubmitting:false,pendingAppliedQuery:"",pendingAppliedDimensionMode:"any",pendingAsOf:"",productionDetailRouteId:null}};
let resolvePending;
function api() {{ return new Promise(resolve => {{ resolvePending = resolve; }}); }}
function render() {{}}
function toChineseMessage(error) {{ return String(error?.message || error); }}
function reportIncomingContentReady() {{ throw new Error("unauthorized content was reported ready"); }}
{helpers}
{pending}
(async () => {{
  let resolveAuthorization;
  const authorizationPromise = new Promise(resolve => {{ resolveAuthorization = resolve; }});
  const request = loadPending({{page:1,authorizationPromise}});
  resolvePending({{items:[{{item_id:"secret",remaining_quantity:9}}],total:1,page:1,page_size:20,session_identity_header:"9:1"}});
  await new Promise(resolve => setImmediate(resolve));
  if (state.pending.length) throw new Error("pending rows applied before authorization");
  resolveAuthorization(false);
  if (await request !== false) throw new Error("denied authorization reported success");
  if (state.pending.length || state.pendingTotal) throw new Error("denied rows entered state");
}})().catch(error => {{ console.error(error); process.exit(1); }});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_parallel_initial_reads_reject_mismatched_session_identity(tmp_path: Path) -> None:
    script_match = re.search(r"<script>(.*?)</script>", INCOMING, re.DOTALL)
    assert script_match is not None
    script = script_match.group(1)

    def between(start: str, end: str) -> str:
        begin = script.index(start)
        return script[begin : script.index(end, begin)]

    reset = between("function resetAuthenticatedIncomingState(", "function beginLatestRequest(key)")
    helpers = between("function beginLatestRequest(key)", "function createIdempotencyKey()")
    pending = between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function changePendingPage",
    )
    node = shutil.which("node")
    assert node
    target = tmp_path / "candidate-c-session-identity.js"
    target.write_text(
        f"""
class FakeAbortController {{ constructor(){{this.signal={{aborted:false}};}} abort(){{this.signal.aborted=true;}} }}
global.AbortController=FakeAbortController;
const requestControllers=new Map();
const nodes=new Proxy({{}},{{get:(target,key)=>target[key]||=(key==="pendingDimensionMode"?{{value:"any"}}:key==="pendingSearchInput"?{{value:""}}:{{hidden:true,textContent:"",innerHTML:""}})}});
const $=id=>nodes[id];
const state={{activeTab:"pending",pending:[],pendingTotal:0,pendingPage:1,pendingPageSize:20,pendingRetryPage:1,pendingLoading:false,pendingError:"",pendingAppliedQuery:"",pendingAppliedDimensionMode:"any",pendingAsOf:"",expandedIds:new Set(),authGeneration:1,user:{{id:2}},permissions:["incoming.view"],sessionIdentity:{{user_id:2,auth_version:4}},initialContentReported:false,performanceRequests:[],received:[],receivedLoaded:false,receivedLoading:false,receivedError:"",locations:[],locationsLoading:false,busyItemIds:new Set(),receiveIdempotencyKeys:new Map(),revertingItemId:null,revertSubmitting:false,productionDetailRouteId:null}};
async function api(){{return {{items:[{{item_id:"foreign",remaining_quantity:9}}],total:1,page:1,page_size:20,session_identity_header:"1:7"}};}}
function render(){{}} function toChineseMessage(error){{return String(error?.message||error);}} function reportIncomingContentReady(){{throw new Error("foreign content ready");}}
{reset}
{helpers}
{pending}
(async()=>{{
 const result=await loadPending({{page:1,authorizationPromise:Promise.resolve(true)}});
 if(result!==false)throw new Error("mismatched identity reported success");
 if(state.pending.length||state.pendingTotal)throw new Error("foreign rows entered state");
 if(state.user!==null||state.sessionIdentity!==null)throw new Error("mismatched session was not cleared");
}})().catch(error=>{{console.error(error);process.exit(1);}});
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def test_session_identity_is_returned_by_auth_and_pending_contracts() -> None:
    auth = (ROOT / "app" / "api" / "auth.py").read_text(encoding="utf-8")
    incoming_api = (ROOT / "app" / "api" / "incoming.py").read_text(encoding="utf-8")
    mobile_api = (ROOT / "app" / "api" / "mobile_erp.py").read_text(encoding="utf-8")
    assert '"session_identity"' in auth
    assert '"user_id": user.id' in auth
    assert '"auth_version": user.auth_version' in auth
    for source in (incoming_api, mobile_api):
        assert 'response.headers["X-ERP-Session-Identity"]' in source
        assert 'f"{user.id}:{user.auth_version}"' in source
    pending_signature = incoming_api.split("def pending_items(", 1)[1].split(") -> dict:", 1)[0]
    assert "response: Response" in pending_signature
    assert "response: Response = None" not in pending_signature


def test_later_pending_refresh_also_rejects_cookie_identity_switch(tmp_path: Path) -> None:
    script_match = re.search(r"<script>(.*?)</script>", INCOMING, re.DOTALL)
    assert script_match is not None
    script = script_match.group(1)

    def between(start: str, end: str) -> str:
        begin = script.index(start)
        return script[begin : script.index(end, begin)]

    reset = between("function resetAuthenticatedIncomingState(", "function beginLatestRequest(key)")
    helpers = between("function beginLatestRequest(key)", "function createIdempotencyKey()")
    pending = between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function changePendingPage",
    )
    node = shutil.which("node")
    assert node
    target = tmp_path / "candidate-c-refresh-session-switch.js"
    target.write_text(
        f"""
class FakeAbortController {{ constructor(){{this.signal={{aborted:false}};}} abort(){{this.signal.aborted=true;}} }}
global.AbortController=FakeAbortController;
const requestControllers=new Map();
const nodes=new Proxy({{}},{{get:(target,key)=>target[key]||=(key==="pendingDimensionMode"?{{value:"any"}}:key==="pendingSearchInput"?{{value:""}}:{{hidden:true,textContent:"",innerHTML:""}})}});
const $=id=>nodes[id];
const state={{activeTab:"pending",pending:[{{item_id:"a-last-good"}}],pendingTotal:1,pendingPage:1,pendingPageSize:20,pendingRetryPage:1,pendingLoading:false,pendingError:"",pendingAppliedQuery:"",pendingAppliedDimensionMode:"any",pendingAsOf:"",expandedIds:new Set(),authGeneration:4,user:{{id:1}},permissions:["incoming.view"],sessionIdentity:{{user_id:1,auth_version:3}},initialContentReported:true,performanceRequests:[],received:[],receivedLoaded:false,receivedLoading:false,receivedError:"",locations:[],locationsLoading:false,busyItemIds:new Set(),receiveIdempotencyKeys:new Map(),revertingItemId:null,revertSubmitting:false,productionDetailRouteId:null}};
async function api(){{return {{items:[{{item_id:"b-secret",remaining_quantity:9}}],total:1,page:1,page_size:20,session_identity_header:"2:8"}};}}
function render(){{}} function toChineseMessage(error){{return String(error?.message||error);}}
{reset}
{helpers}
{pending}
(async()=>{{
 const result=await loadPending({{page:1}});
 if(result!==false)throw new Error("cross-account refresh reported success");
 if(state.pending.some(row=>row.item_id==="b-secret"))throw new Error("cross-account rows entered state");
 if(state.user!==null||state.sessionIdentity!==null)throw new Error("cross-account state was not cleared");
}})().catch(error=>{{console.error(error);process.exit(1);}});
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr

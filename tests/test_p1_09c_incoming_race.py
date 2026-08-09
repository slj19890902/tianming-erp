from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*?)</script>", MOBILE, re.DOTALL)
    assert match is not None
    return match.group(1)


def _between(start: str, end: str) -> str:
    source = _script()
    return source[source.index(start) : source.index(end, source.index(start))]


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


def test_mobile_incoming_has_latest_only_and_duplicate_submit_guards() -> None:
    for marker in (
        "function isLatestRequest(key, controller)",
        'isLatestRequest("incoming:pending", controller)',
        'isLatestRequest("incoming:received", controller)',
        "busyItemIds: new Set()",
        "receiveIdempotencyKeys: new Map()",
        "if (state.busyItemIds.has(key)) return;",
        "state.busyItemIds.add(key)",
        "state.busyItemIds.delete(key)",
        'id="confirmRevert"',
        "state.revertSubmitting",
        "retryCurrentView",
    ):
        assert marker in MOBILE

    assert 'data-accept-short="${key}" ${disabled ? "disabled" : ""}' in MOBILE
    assert 'idempotency_key: operationKey' in MOBILE


def test_older_pending_response_cannot_hide_or_replace_latest_request(
    tmp_path: Path,
) -> None:
    helpers = _between("function beginLatestRequest(key)", "function createIdempotencyKey()")
    load_pending = _between(
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function loadReceived",
    )
    harness = f"""
class FakeAbortController {{
  constructor() {{ this.signal = {{aborted:false}}; }}
  abort() {{ this.signal.aborted = true; }}
}}
global.AbortController = FakeAbortController;
const requestControllers = new Map();
const nodes = {{
  loadingState: {{hidden:true}}, errorState: {{hidden:true}}, cardList: {{innerHTML:""}},
  emptyState: {{hidden:true}}, errorMessage: {{textContent:""}}, authNotice: {{hidden:true}}
}};
const $ = id => nodes[id];
const state = {{pending:[], expandedIds:new Set()}};
const calls = [];
function api(url, options) {{
  return new Promise((resolve, reject) => calls.push({{resolve, reject, signal:options.signal}}));
}}
function render() {{}}
function toChineseMessage(error) {{ return String(error?.message || error); }}
{helpers}
{load_pending}
(async () => {{
  const first = loadPending();
  const second = loadPending();
  calls[0].resolve({{items:[{{item_id:1, remaining_quantity:1}}]}});
  await first;
  if (nodes.loadingState.hidden) throw new Error("older request hid the active loading state");
  if (state.pending.length) throw new Error("older response overwrote current data");
  calls[1].resolve({{items:[{{item_id:2, remaining_quantity:2}}]}});
  await second;
  if (!nodes.loadingState.hidden) throw new Error("latest request did not clear loading state");
  if (state.pending[0]?.item_id !== 2) throw new Error("latest response was not retained");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(harness, tmp_path, "p1-09c-incoming-pending-race.js")


def test_mobile_incoming_error_parser_handles_objects_and_non_json(
    tmp_path: Path,
) -> None:
    formatter = _between("function formatApiDetail(detail)", "async function api(url")
    api_source = _between("async function api(url", "async function checkAuth()")
    harness = f"""
const state = {{user:{{id:1}},permissions:["incoming.view"]}};
const nodes = {{authNotice:{{hidden:true}}}};
const $ = id => nodes[id];
{formatter}
{api_source}
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
(async () => {{
  global.fetch = async () => ({{
    ok:false, status:409,
    text:async () => JSON.stringify({{detail:{{message:"库存版本已变化", code:"STALE_VERSION"}}}})
  }});
  try {{ await api("/object-error"); }} catch (error) {{
    expect(error.message.includes("库存版本已变化"), "object detail message missing");
    expect(error.message.includes("STALE_VERSION"), "object detail code missing");
  }}
  global.fetch = async () => ({{ok:false, status:500, text:async () => "upstream exploded"}});
  try {{ await api("/non-json"); }} catch (error) {{
    expect(error.message.includes("服务器内部错误"), "500 explanation missing");
    expect(error.message.includes("HTTP 500"), "HTTP status missing");
    return;
  }}
  throw new Error("non-json request must fail");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(harness, tmp_path, "p1-09c-incoming-errors.js")


def test_mobile_incoming_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    target = tmp_path / "incoming.js"
    target.write_text(_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

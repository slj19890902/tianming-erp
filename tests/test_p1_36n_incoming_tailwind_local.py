from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
INCOMING_PATH = ROOT / "static" / "incoming.html"
CSS_PATH = ROOT / "static" / "vendor" / "tailwindcss-3.4.17-incoming.min.css"
LICENSE_PATH = ROOT / "static" / "vendor" / "tailwindcss-3.4.17.LICENSE"
VENDOR_README_PATH = ROOT / "static" / "vendor" / "README.md"
TOOL_ROOT = ROOT / "tools" / "incoming-tailwind"


def _incoming() -> str:
    return INCOMING_PATH.read_text(encoding="utf-8")


def _inline_script() -> str:
    match = re.search(r"<script>\s*(.*?)\s*</script>", _incoming(), re.DOTALL)
    assert match is not None
    return match.group(1)


def _css_selector(class_name: str) -> str:
    escaped = "".join(
        character
        if character.isalnum() or character in {"-", "_"}
        else f"\\{character}"
        for character in class_name
    )
    return f".{escaped}"


def _run_node(source: str, tmp_path: Path, filename: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the isolated incoming-page behavior test"
    target = tmp_path / filename
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_uses_only_pinned_same_origin_tailwind_asset() -> None:
    incoming = _incoming()
    head = incoming.split("</head>", 1)[0]
    runtime_tags = re.findall(r"<(?:script|link)\b[^>]*>", head, re.IGNORECASE)
    runtime_urls = [
        url
        for tag in runtime_tags
        for url in re.findall(r"(?:src|href)=[\"']([^\"']+)", tag)
    ]

    assert "cdn.tailwindcss.com" not in incoming
    assert all(not re.match(r"https?://", url, re.IGNORECASE) for url in runtime_urls)
    assert all(url.startswith("/static/") for url in runtime_urls)
    assert not re.search(
        r"<script\b[^>]*(?:tailwind|cdn)[^>]*>", head, re.IGNORECASE
    )

    css_bytes = CSS_PATH.read_bytes()
    digest = hashlib.sha256(css_bytes).hexdigest()
    expected_href = (
        "/static/vendor/tailwindcss-3.4.17-incoming.min.css"
        f"?v={digest[:12]}"
    )
    link = re.search(
        r'<link\s+rel=["\']stylesheet["\']\s+href=["\']([^"\']+)["\']\s*/?>',
        head,
        re.IGNORECASE,
    )
    assert link is not None
    assert link.group(1) == expected_href
    assert head.index("</style>") < head.index(link.group(0))

    readme = VENDOR_README_PATH.read_text(encoding="utf-8")
    license_text = LICENSE_PATH.read_text(encoding="utf-8")
    assert "tailwindcss-3.4.17-incoming.min.css" in readme
    assert "tailwindcss@3.4.17" in readme
    assert digest.upper() in readme.upper()
    assert "npm ci" in readme and "npm run build" in readme
    assert "MIT License" in license_text
    assert "Copyright" in license_text


def test_tailwind_build_chain_is_locked_and_scans_only_incoming() -> None:
    package = json.loads((TOOL_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((TOOL_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    config = (TOOL_ROOT / "tailwind.config.cjs").read_text(encoding="utf-8")
    source = (TOOL_ROOT / "src" / "incoming.css").read_text(encoding="utf-8")

    declared = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
    }
    assert declared == {"tailwindcss": "3.4.17"}
    assert package.get("private") is True
    build = package.get("scripts", {}).get("build", "")
    assert "tailwindcss" in build
    assert "--minify" in build
    assert "tailwindcss-3.4.17-incoming.min.css" in build

    assert lock.get("lockfileVersion") == 3
    root_package = lock["packages"][""]
    locked_declared = {
        **root_package.get("dependencies", {}),
        **root_package.get("devDependencies", {}),
    }
    assert locked_declared == {"tailwindcss": "3.4.17"}
    locked_tailwind = lock["packages"]["node_modules/tailwindcss"]
    assert locked_tailwind["version"] == "3.4.17"
    assert locked_tailwind["integrity"].startswith("sha512-")
    assert "tailwindcss-3.4.17.tgz" in locked_tailwind["resolved"]

    content_values = re.findall(r"[\"']([^\"']+\.html)[\"']", config)
    assert content_values == ["../../static/incoming.html"]
    compact_source = re.sub(r"\s+", "", source)
    assert compact_source == (
        "@tailwindbase;@tailwindcomponents;@tailwindutilities;"
    )


def test_compiled_css_covers_dynamic_responsive_and_interaction_states() -> None:
    incoming = _incoming()
    css = CSS_PATH.read_text(encoding="utf-8")
    compact_css = re.sub(r"\s+", "", css)

    fallback = re.search(r"<style>(.*?)</style>", incoming, re.DOTALL)
    assert fallback is not None
    assert re.search(
        r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important\s*;?\s*\}",
        fallback.group(1),
        re.IGNORECASE,
    )

    required_classes = {
        # Runtime-selected toast, tab, pending/received card, and status classes.
        "bg-red-700",
        "bg-slate-900",
        "bg-blue-700",
        "text-white",
        "bg-slate-200",
        "text-slate-700",
        "border-orange-500",
        "border-green-600",
        "bg-orange-100",
        "text-orange-800",
        "bg-green-100",
        "text-green-800",
        # Modal, drawing viewer, toast positioning, and arbitrary width.
        "fixed",
        "inset-0",
        "z-50",
        "items-end",
        "bg-black/60",
        "bottom-5",
        "left-1/2",
        "-translate-x-1/2",
        "w-[calc(100%-2rem)]",
        "overflow-auto",
        "h-full",
        # Active and disabled variants used by real buttons and inputs.
        "active:bg-blue-700",
        "active:bg-indigo-800",
        "active:bg-red-800",
        "active:bg-slate-50",
        "disabled:bg-slate-200",
        "disabled:bg-slate-400",
        "disabled:cursor-not-allowed",
        "disabled:opacity-50",
        # Every responsive variant currently present in incoming.html.
        "sm:items-center",
        "sm:justify-center",
        "sm:max-w-lg",
        "sm:rounded-2xl",
    }
    missing = sorted(
        class_name
        for class_name in required_classes
        if _css_selector(class_name) not in css
    )
    assert missing == []
    assert "@media(min-width:640px)" in compact_css

    raw_size = len(CSS_PATH.read_bytes())
    gzip_size = len(gzip.compress(CSS_PATH.read_bytes(), compresslevel=6))
    assert 4_000 < raw_size < 80_000
    assert gzip_size < 20_000
    assert gzip_size < raw_size


def test_html_and_css_use_existing_cache_gzip_etag_contract() -> None:
    from app.main import app

    css_url = "/static/vendor/tailwindcss-3.4.17-incoming.min.css"
    with TestClient(app) as client:
        html_response = client.get(
            "/incoming.html", headers={"Accept-Encoding": "gzip"}
        )
        css_response = client.get(css_url, headers={"Accept-Encoding": "gzip"})
        not_modified = client.get(
            css_url,
            headers={
                "Accept-Encoding": "gzip",
                "If-None-Match": css_response.headers["etag"],
            },
        )

    assert html_response.status_code == 200
    assert "no-store" in html_response.headers["cache-control"]
    assert css_response.status_code == 200
    assert css_response.headers["content-type"].startswith("text/css")
    assert css_response.headers["content-encoding"] == "gzip"
    assert css_response.headers["vary"] == "Accept-Encoding"
    assert css_response.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )
    assert css_response.headers["etag"]
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == css_response.headers["etag"]
    assert not_modified.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )


def test_inline_runtime_keeps_requests_and_key_interactions(tmp_path: Path) -> None:
    node_prelude = r'''
const assert = (condition, message) => { if (!condition) throw new Error(message); };
class FakeNode {
  constructor(id = "") {
    this.id = id;
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.innerHTML = "";
    this.className = "";
    this.dataset = {};
    this.children = [];
    this.listeners = {};
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  removeAttribute(name) { delete this[name]; }
}
const nodes = new Proxy({}, { get(target, key) { return target[key] ||= new FakeNode(String(key)); } });
const tabs = [new FakeNode("pending-tab"), new FakeNode("received-tab")];
tabs[0].dataset.tab = "pending";
tabs[1].dataset.tab = "received";
global.document = {
  getElementById(id) { return nodes[id]; },
  querySelectorAll(selector) { return selector === ".tab-button" ? tabs : []; },
};
global.window = globalThis;
window.location = {href: "http://localhost/incoming.html"};
global.TmTime = {formatBeijingDateTime(value) { return String(value || ""); }};
const fetchCalls = [];
function response(payload) {
  return {ok:true,status:200,headers:{get(name){return name==="X-ERP-Session-Identity"?"7:1":"";}},text:async()=>JSON.stringify(payload)};
}
global.fetch = async (url, options) => {
  fetchCalls.push({url:String(url), options});
  if (String(url).startsWith("/api/auth/me")) {
    return response({user:{id:7,real_name:"测试管理员",role:"admin"},session_identity:{user_id:7,auth_version:1},permissions:["incoming.view","incoming.execute"]});
  }
  if (String(url).startsWith("/api/incoming/pending")) {
    return response({items:[],total:0,page:1,page_size:25});
  }
  if (String(url).startsWith("/api/incoming/received")) {
    return response({items:[]});
  }
  throw new Error(`unexpected business request: ${url}`);
};
const cleanUrl = url => String(url).replace(/([?&])_=\d+$/, "").replace(/[?&]$/, "");
async function settle() {
  for (let index = 0; index < 8; index += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
}
'''
    node_assertions = r'''
(async () => {
  await settle();
  assert(fetchCalls.length === 2, `cold init added requests: ${fetchCalls.map(row=>row.url)}`);
  assert(cleanUrl(fetchCalls[0].url) === "/api/auth/me", "auth request changed");
  assert(cleanUrl(fetchCalls[1].url) === "/api/incoming/pending?page=1&page_size=20", "pending request changed");
  assert(fetchCalls.every(row => row.options.cache === "no-store"), "business request cache guard changed");

  await tabs[1].listeners.click();
  assert(state.activeTab === "received", "received tab did not activate");
  assert(tabs[1].className.includes("bg-blue-700") && tabs[1].className.includes("text-white"), "active tab class missing");
  assert(tabs[0].className.includes("bg-slate-200") && tabs[0].className.includes("text-slate-700"), "inactive tab class missing");
  assert(fetchCalls.length === 3 && cleanUrl(fetchCalls[2].url) === "/api/incoming/received", "tab added or changed business requests");

  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  let hideToast = null;
  window.setTimeout = callback => { hideToast = callback; return 1; };
  window.clearTimeout = () => {};
  showToast("失败提示", true);
  assert(nodes.toast.hidden === false && nodes.toast.className.includes("bg-red-700"), "error toast style/state missing");
  hideToast();
  assert(nodes.toast.hidden === true, "toast did not hide");
  window.setTimeout = originalSetTimeout;
  window.clearTimeout = originalClearTimeout;

  state.activeTab = "pending";
  state.busyItemIds.add("pending-1");
  const pendingHtml = renderCard({
    history_key:"pending-1", item_id:"pending-1", incoming_quantity:5,
    planned_quantity:5, cumulative_received_quantity:0,
    pending_receipt_item_id:91, source_type:"order", customer_name:"匿名客户",
    product_code:"P1-36N", product_name:"匿名产品", drawing_path:"",
  });
  assert(pendingHtml.includes("border-orange-500"), "pending card border missing");
  assert(pendingHtml.includes("data-receive=\"pending-1\" disabled"), "busy receive button is not disabled");

  state.activeTab = "received";
  const receivedItem = {
    history_key:"92", item_id:"received-1", receipt_item_id:92,
    incoming_quantity:5, planned_quantity:5, cumulative_received_quantity:5,
    customer_name:"匿名客户", product_code:"P1-36N", product_name:"匿名产品",
    drawing_path:"/static/drawing.pdf", drawing_is_pdf:true, can_revert_receipt:true,
  };
  state.received = [receivedItem];
  state.expandedIds.add("92");
  const receivedHtml = renderCard(receivedItem);
  assert(receivedHtml.includes("border-green-600"), "received card border missing");
  assert(receivedHtml.includes("data-revert=\"92\""), "admin revert action missing");

  openRevert("92");
  assert(nodes.revertModal.hidden === false && state.revertingItemId === "92", "revert modal did not open");
  openDrawing("92");
  assert(nodes.drawingViewer.hidden === false, "drawing viewer did not open");
  assert(nodes.drawingViewerFrame.hidden === false && nodes.drawingViewerImage.hidden === true, "PDF viewer selection changed");
  closeDrawing();
  assert(nodes.drawingViewer.hidden === true, "drawing viewer did not close");
})().catch(error => { console.error(error); process.exit(1); });
'''
    _run_node(
        node_prelude + "\n" + _inline_script() + "\n" + node_assertions,
        tmp_path,
        "p1-36n-incoming-runtime.js",
    )


def test_inline_javascript_is_valid_and_business_endpoints_are_unchanged(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    script_path = tmp_path / "incoming-p1-36n.js"
    script_path.write_text(_inline_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    incoming = _incoming()
    endpoint_markers = (
        "/api/auth/me",
        'endpoint = "/api/incoming/pending"',
        "/api/incoming/received",
        "/api/incoming/surplus-locations",
        "/api/incoming/receive/${item.item_id}",
        "/api/incoming/receipt-items/${item.pending_receipt_item_id}/accept-short",
        "/api/incoming/receipt-items/${item.receipt_item_id}/revert",
        "/api/incoming/revert/${item?.item_id ?? itemId}",
    )
    for marker in endpoint_markers:
        assert incoming.count(marker) == 1, marker

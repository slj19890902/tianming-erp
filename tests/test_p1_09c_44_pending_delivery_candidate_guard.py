from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(start: str, end: str) -> str:
    start_index = INDEX.index(start) + len(start)
    body = INDEX[start_index : INDEX.index(end, start_index)]
    return re.sub(r"\n\s*},\s*$", "", body)


def _run_node(tmp_path: Path, name: str, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for pending delivery candidate regression"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_full_pending_loader_ignores_stale_results_and_preserves_last_success(
    tmp_path: Path,
) -> None:
    body = _method_body(
        "async loadDeliveryPendingItems() {",
        "async loadDeliveryCustomerOptions() {",
    )
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const requests = [];
global.axios = {{ get(url) {{ return new Promise((resolve, reject) => requests.push({{url,resolve,reject}})); }} }};
const vm = {{
  deliveryPendingState:{{loading:false,error:"",request_token:0}},
  pendingDeliveryItems:[], deliveryCustomerCandidates:[],
  errorMessage(error) {{ return error?.message || String(error); }},
}};
vm.load = new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const first = vm.load();
  const second = vm.load();
  if (!vm.deliveryPendingState.loading || vm.deliveryPendingState.error) throw new Error("latest loading state is wrong");
  requests[1].resolve({{data:{{items:[{{item_id:2}}],customer_candidates:[{{customer_id:2}}]}}}});
  if (await second !== true) throw new Error("latest request did not report success");
  requests[0].resolve({{data:{{items:[{{item_id:1}}],customer_candidates:[{{customer_id:1}}]}}}});
  if (await first !== false) throw new Error("stale success was not rejected");
  if (vm.pendingDeliveryItems[0].item_id !== 2 || vm.deliveryCustomerCandidates[0].customer_id !== 2) throw new Error("stale success overwrote latest candidates");

  const staleFailure = vm.load();
  const latestSuccess = vm.load();
  requests[3].resolve({{data:{{items:[{{item_id:3}}],customer_candidates:[{{customer_id:3}}]}}}});
  await latestSuccess;
  requests[2].reject(new Error("旧请求失败"));
  await staleFailure;
  if (vm.deliveryPendingState.error || vm.pendingDeliveryItems[0].item_id !== 3) throw new Error("stale failure leaked into latest candidates");

  const previousItems = vm.pendingDeliveryItems;
  const previousCustomers = vm.deliveryCustomerCandidates;
  const currentFailure = vm.load();
  requests[4].reject(new Error("当前网络断开"));
  if (await currentFailure !== false) throw new Error("current failure did not report false");
  if (vm.pendingDeliveryItems !== previousItems || vm.deliveryCustomerCandidates !== previousCustomers) throw new Error("current failure discarded the last successful candidates");
  if (!vm.deliveryPendingState.error.includes("当前网络断开") || vm.deliveryPendingState.loading) throw new Error("current failure state is wrong");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "p1-09c-44-pending-race.js", source)


def test_pending_customer_panel_distinguishes_loading_failure_and_real_empty() -> None:
    start = INDEX.index("deliveryDashboardMode==='pending_customers'")
    end = INDEX.index('<div class="toolbar">', start + 1000)
    panel = INDEX[start:end]
    for marker in (
        "deliveryPendingState.loading",
        "deliveryPendingState.error",
        "正在加载待送货客户",
        "待送货客户加载失败",
        '@click="loadDeliveryPendingItems"',
        "当前没有待送货客户",
    ):
        assert marker in panel


def test_existing_order_delivery_draft_does_not_open_without_candidates() -> None:
    body = _method_body("async editDelivery(row) {", "async deleteDelivery(row) {")
    load_index = body.index("await this.loadDeliveryPendingItems()")
    guard_index = body.index("if (!pendingLoaded)")
    modal_index = body.index('this.modal = { type:"delivery"')
    assert load_index < guard_index < modal_index
    assert "送货明细加载失败" in body
    assert "return false" in body[guard_index:modal_index]
    assert "return true" in body[modal_index:]


def test_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-44-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

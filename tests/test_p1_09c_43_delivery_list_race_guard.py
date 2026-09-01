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


def _run_node(tmp_path: Path, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for delivery list race regression"
    target = tmp_path / "p1-09c-43-delivery-list-race.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_delivery_list_loader_freezes_params_and_ignores_stale_responses(tmp_path: Path) -> None:
    body = _method_body("async loadDeliveries() {", "async loadDeliveryPendingItems() {")
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const requests = [];
global.axios = {{ get(url, config) {{ return new Promise((resolve, reject) => requests.push({{url,config,resolve,reject}})); }} }};
const vm = {{
  pages:{{deliveries:1}}, pageSize:25,
  deliveryListFilters:{{customer_id:1,delivery_no:"OLD",status:"pending",product_code:""}},
  deliveryDetailedFilters:{{customer_po:"",product_code:"",product_name:""}},
  deliveryListPageSize() {{ return 10; }},
  deliveryListState:{{initialLoading:false,refreshing:false,error:"",loaded:false,request_token:0}},
  deliveries:[], deliveriesTotal:0,
  errorMessage(error) {{ return error?.message || String(error); }},
}};
vm.loadDeliveries = new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const first = vm.loadDeliveries();
  if (!vm.deliveryListState.initialLoading || vm.deliveryListState.refreshing) throw new Error("first load state is wrong");
  vm.pages.deliveries = 2;
  vm.deliveryListFilters = {{customer_id:2,delivery_no:"NEW",status:"dispatched",product_code:"P2"}};
  const second = vm.loadDeliveries();
  if (requests[0].config.params.page !== 1 || requests[0].config.params.customer_id !== 1 || requests[0].config.params.delivery_no !== "OLD") throw new Error("first request params were not frozen");
  if (requests[1].config.params.page !== 2 || requests[1].config.params.customer_id !== 2 || requests[1].config.params.delivery_no !== "NEW") throw new Error("second request did not use current filters");
  requests[1].resolve({{data:{{items:[{{id:2,delivery_number:"NEW"}}],total:1}}}});
  if (await second !== true) throw new Error("latest request did not report success");
  requests[0].resolve({{data:{{items:[{{id:1,delivery_number:"OLD"}}],total:99}}}});
  if (await first !== false) throw new Error("stale request was not rejected");
  if (vm.deliveries.length !== 1 || vm.deliveries[0].id !== 2 || vm.deliveriesTotal !== 1) throw new Error("stale success overwrote latest list");
  if (vm.deliveryListState.initialLoading || vm.deliveryListState.refreshing || vm.deliveryListState.error || !vm.deliveryListState.loaded) throw new Error("latest success left incorrect list state");

  vm.deliveryListState.loaded = true;
  vm.deliveryListState.error = "";
  const staleFailure = vm.loadDeliveries();
  vm.deliveryListFilters.delivery_no = "LATEST";
  const latestSuccess = vm.loadDeliveries();
  if (!vm.deliveryListState.refreshing || vm.deliveryListState.initialLoading) throw new Error("refresh must preserve the prior list");
  requests[3].resolve({{data:{{items:[{{id:3,delivery_number:"LATEST"}}],total:1}}}});
  await latestSuccess;
  requests[2].reject(new Error("旧请求失败"));
  await staleFailure;
  if (vm.deliveryListState.error || vm.deliveries[0].id !== 3) throw new Error("stale failure leaked into latest result");

  const previous = vm.deliveries;
  const currentFailure = vm.loadDeliveries();
  requests[4].reject(new Error("当前网络断开"));
  if (await currentFailure !== false) throw new Error("current failure did not report false");
  if (vm.deliveries !== previous || !vm.deliveryListState.error.includes("当前网络断开")) throw new Error("current failure did not preserve rows and expose error");
  if (vm.deliveryListState.initialLoading || vm.deliveryListState.refreshing) throw new Error("current failure did not release loading state");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, source)


def test_delivery_list_loading_contract_remains_visible() -> None:
    assert 'v-if="deliveryListState.initialLoading"' in INDEX
    assert 'v-if="deliveryListState.refreshing"' in INDEX
    assert "已保留上次列表结果" in INDEX
    loader = _method_body("async loadDeliveries() {", "async loadDeliveryPendingItems() {")
    assert "requestToken !== this.deliveryListState.request_token" in loader
    assert "this.deliveries = data.items || []" in loader
    assert "this.deliveryListState.error = this.errorMessage(error)" in loader


def test_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-43-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

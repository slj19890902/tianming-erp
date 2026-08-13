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


def test_supplier_list_has_latest_request_and_retry_contract() -> None:
    block = INDEX.split("async loadSuppliers(", 1)[1].split("defaultSupplierName()", 1)[0]
    for marker in (
        'const requestKey = "suppliers:list";',
        "this.beginLatestRequest(requestKey)",
        "signal:controller.signal",
        "latestRequestControllers.get(requestKey) !== controller",
        "this.isCancelledRequest(error)",
        "this.finishLatestRequest(requestKey, controller)",
    ):
        assert marker in block
    assert '@click="loadSuppliers(true)"' in INDEX
    assert "重新加载" in INDEX


def test_supplier_list_old_response_cannot_overwrite_latest_result(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the supplier race regression")

    body = _method_body(
        "async loadSuppliers(includeInactive = this.canAdmin) {",
        "defaultSupplierName() {",
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.latestRequestControllers = new Map();
const pending = [];
globalThis.axios = {{
  get(url, options) {{
    return new Promise((resolve, reject) => pending.push({{resolve, reject, options}}));
  }}
}};
const vm = {{
  canAdmin: true,
  suppliers: [{{standard_name:"原列表", is_active:true}}],
  materialSuppliers: [],
  supplierLoading: false,
  supplierError: "",
  hasPermission(permission) {{ return permission === "products.view"; }},
  get activeSupplierNames() {{ return this.suppliers.filter(row => row.is_active !== false).map(row => row.standard_name); }},
  beginLatestRequest(key) {{
    latestRequestControllers.get(key)?.abort();
    const controller = new AbortController();
    latestRequestControllers.set(key, controller);
    return controller;
  }},
  finishLatestRequest(key, controller) {{ if (latestRequestControllers.get(key) === controller) latestRequestControllers.delete(key); }},
  isCancelledRequest(error) {{ return error?.name === "AbortError" || error?.code === "ERR_CANCELED"; }},
  errorMessage(error) {{ return error?.message || String(error); }}
}};
vm.loadSuppliers = new AsyncFunction("includeInactive", {json.dumps(body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const first = vm.loadSuppliers(true);
  const second = vm.loadSuppliers(true);
  pending[1].resolve({{data:{{items:[{{standard_name:"最新供应商", is_active:true}}]}}}});
  await second;
  pending[0].resolve({{data:{{items:[{{standard_name:"旧供应商", is_active:true}}]}}}});
  await first;
  if (vm.suppliers.length !== 1 || vm.suppliers[0].standard_name !== "最新供应商") throw new Error("old supplier response overwrote latest list");
  if (vm.supplierLoading) throw new Error("latest loading state did not finish");

  const failed = vm.loadSuppliers(true);
  pending[2].reject(new Error("网络断开"));
  await failed;
  if (vm.suppliers.length !== 0) throw new Error("failed current request retained stale suppliers");
  if (!vm.supplierError.includes("网络断开")) throw new Error("current failure was not shown");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "supplier-list-race.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_supplier_search_matches_historical_aliases(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the supplier alias search regression")
    body = _method_body(
        "filteredSuppliers() {",
        "activeExternalPackagingCategoryOptions() {",
    )
    script = f"""
const vm = {{
  supplierSearch: "旧供应商简称",
  supplierGroupFilter: "all",
  suppliers: [{{
    id: 1,
    standard_name: "供应商正式全称",
    display_name: "正式简称",
    business_code: "NEW",
    aliases: ["旧供应商简称"],
    sort_order: 1
  }}]
}};
vm.filteredSuppliers = new Function({json.dumps(body, ensure_ascii=False)}).bind(vm);
const rows = vm.filteredSuppliers();
if (rows.length !== 1 || rows[0].id !== 1) throw new Error("supplier alias was not searchable");
"""
    target = tmp_path / "supplier-alias-search.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_supplier_frontend_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr

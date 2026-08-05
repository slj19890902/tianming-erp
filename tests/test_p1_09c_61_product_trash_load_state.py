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
    assert node is not None, "Node.js is required for the product trash loading regression"
    target = tmp_path / "product-trash-load-state.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_product_trash_template_has_loading_error_and_retry_states() -> None:
    assert "productTrashLoading:false" in INDEX
    assert 'productTrashError:""' in INDEX
    assert "正在读取产品垃圾站…" in INDEX
    assert 'v-else-if="productTrashError"' in INDEX
    assert "{{ productTrashError }}" in INDEX
    assert 'productTrashLoading ? "读取中…" : "刷新"' in INDEX
    assert ':disabled="productLifecycleBusy() || productTrashLoading"' in INDEX


def test_product_trash_loader_declares_latest_request_contract() -> None:
    body = _method_body("async loadProductTrash() {", "async restoreProduct(row) {")
    assert 'const requestKey = "product:trash"' in body
    assert "const controller = this.beginLatestRequest(requestKey)" in body
    assert "signal:controller.signal" in body
    assert "latestRequestControllers.get(requestKey) !== controller" in body
    assert "this.isCancelledRequest(error)" in body
    assert "this.productTrashLoading = true" in body
    assert "this.productTrashError = this.errorMessage(error)" in body
    assert "return true" in body and "return false" in body


def test_product_trash_runtime_keeps_latest_success_and_current_error(tmp_path: Path) -> None:
    body = _method_body("async loadProductTrash() {", "async restoreProduct(row) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  productTrashItems:[],productTrashLoading:false,productTrashError:"",
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},
  errorMessage(error){{return error?.message||String(error);}}
}};
vm.loadProductTrash=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadProductTrash();const second=vm.loadProductTrash();
  expect(pending[1].options.signal instanceof AbortSignal,"latest trash request has no abort signal");
  pending[1].resolve({{data:{{items:[{{id:2}}]}}}});expect(await second===true,"latest trash success did not report true");
  pending[0].resolve({{data:{{items:[{{id:1}}]}}}});expect(await first===false,"old trash success was treated as current");
  expect(vm.productTrashItems[0].id===2,"old trash success replaced latest list");
  const oldFailure=vm.loadProductTrash();const latest=vm.loadProductTrash();
  pending[3].resolve({{data:{{items:[{{id:4}}]}}}});expect(await latest===true,"second latest trash request failed");
  pending[2].reject(new Error("旧请求失败"));expect(await oldFailure===false,"old trash failure was treated as current");
  expect(vm.productTrashItems[0].id===4&&vm.productTrashError==="","old trash failure polluted latest state");
  const currentFailure=vm.loadProductTrash();pending[4].reject(new Error("当前读取失败"));
  expect(await currentFailure===false,"current trash failure did not report false");
  expect(vm.productTrashError.includes("当前读取失败"),"current trash failure feedback missing");
  expect(vm.productTrashLoading===false,"current trash failure left loading active");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_opening_product_trash_awaits_the_guarded_loader() -> None:
    body = _method_body("async toggleProductTrash() {", "async loadProductTrash() {")
    assert "if (this.productTrashOpen)" in body
    assert "return await this.loadProductTrash()" in body

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
    assert node is not None, "Node.js is required for the product lifecycle regression"
    target = tmp_path / "product-lifecycle-guard.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_product_lifecycle_buttons_have_single_flight_feedback() -> None:
    assert 'productLifecycleAction:{action:"",productId:null}' in INDEX
    assert INDEX.count(':disabled="productLifecycleBusy()"') >= 4
    assert ':disabled="productLifecycleBusy() || productTrashLoading || !productTrashItems.length"' in INDEX
    assert "productLifecyclePending('status',row.id) ? '处理中…'" in INDEX
    assert "productLifecyclePending('delete',row.id) ? '处理中…'" in INDEX
    assert "productLifecyclePending('restore',row.id) ? '处理中…'" in INDEX
    assert "productLifecyclePending('purge',row.id) ? '处理中…'" in INDEX
    assert "productLifecyclePending('empty') ? '处理中…'" in INDEX


def test_product_lifecycle_runtime_blocks_duplicates_and_releases(tmp_path: Path) -> None:
    body = _method_body(
        "async runProductLifecycleAction({action, productId=null, confirmMessage, task}) {",
        "async deleteCustomer(row) {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function({{action, productId=null, confirmMessage, task}}) {{"+body+"}}");
let confirmCount=0,taskCount=0,toastCount=0,allowConfirm=true,release;
global.confirm=()=>{{confirmCount+=1;return allowConfirm;}};
const vm={{
  productLifecycleAction:{{action:"",productId:null}},
  productLifecycleBusy(){{return Boolean(this.productLifecycleAction?.action);}},
  showToast(){{toastCount+=1;}},
  errorMessage(error){{return error?.message||String(error);}}
}};
vm.runProductLifecycleAction=factory().bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.runProductLifecycleAction({{action:"delete",productId:7,confirmMessage:"确认",task:()=>{{taskCount+=1;return new Promise(resolve=>{{release=resolve;}});}}}});
  const duplicate=await vm.runProductLifecycleAction({{action:"delete",productId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(duplicate===false,"duplicate lifecycle action was not rejected");
  expect(confirmCount===1&&taskCount===1,"duplicate action repeated confirmation or request");
  release(true);expect(await first===true,"first lifecycle action did not complete");
  expect(vm.productLifecycleAction.action==="","success did not release lifecycle lock");
  const failed=await vm.runProductLifecycleAction({{action:"purge",productId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;throw new Error("失败");}}}});
  expect(failed===false&&toastCount===1,"failure was not reported once");
  expect(vm.productLifecycleAction.action==="","failure did not release lifecycle lock");
  allowConfirm=false;
  const cancelled=await vm.runProductLifecycleAction({{action:"restore",productId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(cancelled===false&&taskCount===2,"cancelled confirmation still executed mutation");
  expect(vm.productLifecycleAction.action==="","cancelled confirmation acquired lifecycle lock");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_all_product_lifecycle_methods_use_shared_guard() -> None:
    methods = {
        "delete": _method_body("async deleteProduct(row) {", "async toggleProductTrash() {"),
        "restore": _method_body("async restoreProduct(row) {", "async purgeProduct(row) {"),
        "purge": _method_body("async purgeProduct(row) {", "async emptyProductTrash() {"),
        "empty": _method_body("async emptyProductTrash() {", "async toggleProductStatus(row) {"),
        "status": _method_body("async toggleProductStatus(row) {", "async deleteMaterial(row) {"),
    }
    for action, body in methods.items():
        assert "this.runProductLifecycleAction({" in body
        assert f'action:"{action}"' in body

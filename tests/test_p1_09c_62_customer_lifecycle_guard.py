from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX, f"missing Vue method: {signature}"
    assert next_signature in INDEX, f"missing Vue method boundary: {next_signature}"
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the customer lifecycle regression"
    target = tmp_path / "customer-lifecycle-guard.js"
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


def test_customer_lifecycle_button_has_single_flight_feedback() -> None:
    assert 'customerLifecycleAction:{action:"",customerId:null}' in INDEX
    assert ':disabled="customerLifecycleBusy()"' in INDEX
    assert "customerLifecyclePending('status',row.id) ? '处理中…'" in INDEX


def test_customer_lifecycle_runtime_blocks_duplicates_and_releases(tmp_path: Path) -> None:
    body = _method_body(
        "async runCustomerLifecycleAction({action, customerId=null, confirmMessage, task}) {",
        "async deleteCustomer(row) {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function({{action, customerId=null, confirmMessage, task}}) {{"+body+"}}");
let confirmCount=0,taskCount=0,toastCount=0,allowConfirm=true,release;
global.confirm=()=>{{confirmCount+=1;return allowConfirm;}};
const vm={{
  customerLifecycleAction:{{action:"",customerId:null}},
  customerLifecycleBusy(){{return Boolean(this.customerLifecycleAction?.action);}},
  showToast(){{toastCount+=1;}},
  errorMessage(error){{return error?.message||String(error);}}
}};
vm.runCustomerLifecycleAction=factory().bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.runCustomerLifecycleAction({{action:"status",customerId:7,confirmMessage:"确认",task:()=>{{taskCount+=1;return new Promise(resolve=>{{release=resolve;}});}}}});
  const duplicate=await vm.runCustomerLifecycleAction({{action:"status",customerId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(duplicate===false,"duplicate lifecycle action was not rejected");
  expect(confirmCount===1&&taskCount===1,"duplicate action repeated confirmation or request");
  release(true);expect(await first===true,"first lifecycle action did not complete");
  expect(vm.customerLifecycleAction.action==="","success did not release lifecycle lock");
  const failed=await vm.runCustomerLifecycleAction({{action:"status",customerId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;throw new Error("失败");}}}});
  expect(failed===false&&toastCount===1,"failure was not reported once");
  expect(vm.customerLifecycleAction.action==="","failure did not release lifecycle lock");
  allowConfirm=false;
  const cancelled=await vm.runCustomerLifecycleAction({{action:"delete",customerId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(cancelled===false&&taskCount===2,"cancelled confirmation still executed mutation");
  expect(vm.customerLifecycleAction.action==="","cancelled confirmation acquired lifecycle lock");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_customer_lifecycle_methods_use_shared_guard_and_keep_versioned_mutation() -> None:
    delete_body = _method_body("async deleteCustomer(row) {", "async toggleCustomerStatus(row) {")
    status_body = _method_body("async toggleCustomerStatus(row) {", "async deleteProduct(row) {")

    for action, body in (("delete", delete_body), ("status", status_body)):
        assert "this.runCustomerLifecycleAction({" in body
        assert f'action:"{action}"' in body
        assert "this.sendVersionedMasterMutation({" in body
    assert "this.loadCustomers()" in status_body
    assert "this.loadCustomerOptions(true)" in status_body

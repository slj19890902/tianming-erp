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


def _business_footer() -> str:
    start = INDEX.index('<div v-if="modal?.type !== \'product\'" class="modal-foot">')
    return INDEX[start : INDEX.index("</div>", start) + 6]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the supplier save regression"
    target = tmp_path / name
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


def test_supplier_save_uses_shared_lock_and_disables_both_footer_actions() -> None:
    footer = _business_footer()
    save = _method_body(
        "async saveModal() {", "async dispatchDelivery(row, options = {}) {"
    )

    cancel_button = re.search(
        r'<button v-if="!isForcedPassword" class="btn" :disabled="([^"]+)" '
        r'@click="closeModal">',
        footer,
    )
    assert cancel_button is not None
    assert "masterSavePending" in cancel_button.group(1)
    assert "['customer','product','material','supplier'].includes(modal?.type) && masterSavePending ? '保存中…'" in footer
    assert 'const masterSaveEntity = ["customer","product","material","supplier"].includes(this.modal?.type)' in save
    assert 'masterSaveEntity === "supplier" ? "供应商"' in save
    assert "正在保存，请勿重复点击" in save


def test_supplier_list_returns_explicit_current_request_result() -> None:
    load = _method_body(
        "async loadSuppliers(includeInactive = this.canAdmin) {", "defaultSupplierName() {"
    )
    assert "latestRequestControllers.get(requestKey) !== controller) return false" in load
    assert "this.materialSuppliers = this.activeSupplierNames;\n              return true;" in load
    assert "return false;" in load


def test_supplier_save_runtime_blocks_duplicate_and_freezes_target(tmp_path: Path) -> None:
    save_body = _method_body(
        "async saveModal() {", "async dispatchDelivery(row, options = {}) {"
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const requests=[];let releaseBase;const notices=[];
global.axios={{
  put(url,payload){{
    if(!releaseBase) return new Promise(resolve=>{{releaseBase=resolve;requests.push({{url,payload}});}});
    requests.push({{url,payload}});return Promise.resolve({{data:{{id:4,version:9,is_active:false}}}});
  }},
  post(url,payload){{requests.push({{url,payload}});return Promise.resolve({{data:{{id:99,version:1,is_active:true}}}});}}
}};
const vm={{
  modal:{{type:"supplier"}},masterSavePending:false,masterPendingSaveOptions:null,
  masterChangeConfirm:{{entity:null}},loading:false,
  supplierForm:{{id:4,version:7,is_active:false,standard_name:"供应商A"}},
  masterCurrentForm(){{return null;}},supplierPayload(){{return {{standard_name:this.supplierForm.standard_name}};}},
  loadSuppliers:async()=>true,closeModal(){{this.modal=null;}},
  showToast(message,error=false){{notices.push([message,error]);}},handleMaster409(){{return false;}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveModal();
  vm.supplierForm={{id:null,version:null,is_active:true,standard_name:"供应商B"}};
  const duplicate=await vm.saveModal();
  expect(duplicate===false,"duplicate supplier save was not rejected");
  expect(requests.length===1&&requests[0].url.endsWith("/4"),"duplicate or changed target issued a write");
  releaseBase({{data:{{id:4,version:8,is_active:true}}}});
  expect(await first===true,"first supplier save did not finish");
  expect(requests.length===2&&requests[1].url.endsWith("/4/status"),"status write did not use frozen supplier");
  expect(requests[1].payload.expected_version===8&&requests[1].payload.is_active===false,"status write lost frozen version/state");
  expect(notices.some(([message])=>message==="供应商已更新"),"result used the changed form identity");
  expect(vm.masterSavePending===false,"supplier save lock was not released");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "supplier-save-lock.js")


def test_supplier_refresh_failure_reports_write_success_without_repeat(tmp_path: Path) -> None:
    refresh_body = _method_body(
        "handleMasterSaveRefreshFailure(entity, error) {",
        "moldRepairWarningText(warnings) {",
    )
    save_body = _method_body(
        "async saveModal() {", "async dispatchDelivery(row, options = {}) {"
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,closed=0;const notices=[];
global.axios={{post:async()=>{{writes+=1;return {{data:{{id:12,version:1,is_active:true}}}};}}}};
const vm={{
  modal:{{type:"supplier"}},masterSavePending:false,masterPendingSaveOptions:null,masterChangeConfirm:{{entity:null}},loading:false,
  supplierForm:{{id:null,version:null,is_active:true,standard_name:"新供应商"}},
  masterCurrentForm(){{return null;}},supplierPayload(){{return {{standard_name:"新供应商"}};}},loadSuppliers:async()=>false,
  supplierError:"供应商读取失败：网络断开",productEditReturnContext:null,
  closeModal(){{closed+=1;this.modal=null;}},showToast(message,error=false){{notices.push([message,error]);}},
  errorMessage(error){{return error?.message||String(error);}},handleMaster409(){{return false;}}
}};
vm.handleMasterSaveRefreshFailure=new Function("entity","error",{json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.saveModal()===true,"refresh failure changed successful supplier write to failure");
  expect(writes===1&&closed===1,"supplier write/close contract failed");
  expect(notices.some(([message,error])=>message.includes("供应商已保存")&&message.includes("业务已成功")&&message.includes("请勿重复提交")&&message.includes("手动刷新核对")&&error===true),"safe supplier refresh warning missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "supplier-refresh-failure.js")


def test_supplier_status_partial_failure_does_not_invite_duplicate_create(tmp_path: Path) -> None:
    save_body = _method_body(
        "async saveModal() {", "async dispatchDelivery(row, options = {}) {"
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,closed=0,refreshes=0;const notices=[];
global.axios={{
  post:async()=>{{writes+=1;return {{data:{{id:15,version:1,is_active:true,standard_name:"新供应商"}}}};}},
  put:async()=>{{writes+=1;throw new Error("状态写入断开");}}
}};
const vm={{
  modal:{{type:"supplier"}},masterSavePending:false,masterPendingSaveOptions:null,masterChangeConfirm:{{entity:null}},loading:false,
  supplierForm:{{id:null,version:null,is_active:false,standard_name:"新供应商"}},
  masterCurrentForm(){{return null;}},supplierPayload(){{return {{standard_name:"新供应商"}};}},
  loadSuppliers:async()=>{{refreshes+=1;return true;}},closeModal(){{closed+=1;this.modal=null;}},
  showToast(message,error=false){{notices.push([message,error]);}},errorMessage(error){{return error?.message||String(error);}},handleMaster409(){{return false;}}
}};
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.saveModal()===false,"partial supplier status failure was reported as full success");
  expect(writes===2&&closed===1&&refreshes===1,"partial result was not closed/refreshed safely");
  expect(notices.some(([message,error])=>message.includes("资料已保存")&&message.includes("状态未生效")&&message.includes("不要重复新建")&&error===true),"partial supplier result guidance missing");
  expect(!notices.some(([message])=>message==="状态写入断开"),"partial result fell through to generic failure");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "supplier-status-partial.js")

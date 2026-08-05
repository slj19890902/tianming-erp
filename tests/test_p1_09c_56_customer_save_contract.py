from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the customer save regression"
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


def test_embedded_customer_save_uses_shared_lock_and_feedback() -> None:
    footer_start = INDEX.index('<div class="modal-foot">')
    footer = INDEX[footer_start : INDEX.index('</div>\n        </div>\n      </div>', footer_start)]
    save = _method_body("async saveModal() {", "async dispatchDelivery(row) {")

    assert "['customer','product','material','supplier'].includes(modal?.type) && masterSavePending ? '保存中…'" in footer
    assert 'const masterSaveEntity = ["customer","product","material","supplier"].includes(this.modal?.type)' in save
    assert 'masterSaveEntity === "customer" ? "客户"' in save
    assert "正在保存，请勿重复点击" in save


def test_embedded_customer_lists_only_accept_latest_response_and_return_result() -> None:
    customers = _method_body("async loadCustomers() {", "async loadCustomerOptions(")
    options = _method_body("async loadCustomerOptions(force=false) {", "async loadProducts() {")
    for block, key in ((customers, "customers:list"), (options, "customers:options")):
        assert f'const requestKey = "{key}"' in block
        assert "latestRequestControllers.get(requestKey) !== controller) return false" in block
        assert "return true;" in block
        assert "if (this.isCancelledRequest(error)) return false" in block


def test_customer_list_runtime_discards_ignored_abort_response(tmp_path: Path) -> None:
    customers_body = _method_body("async loadCustomers() {", "async loadCustomerOptions(")
    options_body = _method_body("async loadCustomerOptions(force=false) {", "async loadProducts() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  filters:{{customerKeyword:"A",showInactive:false}},pages:{{customers:1}},pageSize:25,customers:[],customersTotal:0,customerOptions:[],
  hasPermission(){{return true;}},beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}}
}};
vm.loadCustomers=new AsyncFunction({json.dumps(customers_body, ensure_ascii=False)}).bind(vm);
vm.loadCustomerOptions=new AsyncFunction("force",{json.dumps(options_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadCustomers();vm.filters.customerKeyword="B";const second=vm.loadCustomers();
  pending[1].resolve({{data:{{items:[{{id:2,name:"客户B"}}],total:1}}}});expect(await second===true,"latest customer list did not report success");
  pending[0].resolve({{data:{{items:[{{id:1,name:"客户A"}}],total:1}}}});expect(await first===false,"old customer list was treated as current");
  expect(vm.customers[0].id===2,"old customer list overwrote latest result");
  const optionA=vm.loadCustomerOptions(true);const optionB=vm.loadCustomerOptions(true);
  pending[3].resolve({{data:{{items:[{{id:4,name:"选项B"}}]}}}});expect(await optionB===true,"latest options did not report success");
  pending[2].resolve({{data:{{items:[{{id:3,name:"选项A"}}]}}}});expect(await optionA===false,"old options were treated as current");
  expect(vm.customerOptions[0].id===4,"old customer options overwrote latest result");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "embedded-customer-list-race.js")


def test_customer_preflight_runtime_is_single_flight(tmp_path: Path) -> None:
    save_body = _method_body("async saveModal() {", "async dispatchDelivery(row) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let preflightCalls=0,releasePreflight;const notices=[];
const vm={{
  modal:{{type:"customer"}},masterSavePending:false,masterPendingSaveOptions:null,masterChangeConfirm:{{entity:null}},loading:false,
  customerForm:{{id:7,version:3,name:"客户A"}},masterCurrentForm(){{return this.customerForm;}},masterLocalChanges(){{return [{{field:"name"}}];}},
  prepareCustomerChangeConfirmation(){{preflightCalls+=1;return new Promise(resolve=>releasePreflight=resolve);}},
  showToast(message,error=false){{notices.push([message,error]);}}
}};
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveModal();const duplicate=await vm.saveModal();
  expect(duplicate===false&&preflightCalls===1,"customer duplicate issued a second preflight");
  expect(notices.some(([message])=>message.includes("客户正在保存")),"customer duplicate feedback missing");
  releasePreflight(false);expect(await first===false,"customer preflight result changed");
  expect(vm.masterSavePending===false,"customer save lock was not released");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "customer-preflight-lock.js")


def test_customer_write_target_is_frozen_and_refresh_failure_is_success_warning(
    tmp_path: Path,
) -> None:
    refresh_body = _method_body(
        "handleMasterSaveRefreshFailure(entity, error) {", "async saveModal() {"
    )
    save_body = _method_body("async saveModal() {", "async dispatchDelivery(row) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let releaseWrite;const requests=[],notices=[];let closed=0;
global.axios={{put(url,payload){{requests.push({{url,payload}});return new Promise(resolve=>releaseWrite=resolve);}}}};
const vm={{
  modal:{{type:"customer"}},masterSavePending:false,masterPendingSaveOptions:{{expected_version:4,confirmation_token:"ok"}},masterChangeConfirm:{{entity:null}},loading:false,
  customerForm:{{id:7,version:4,name:"客户A"}},masterCurrentForm(){{return this.customerForm;}},
  buildCustomerWritePayload(){{return {{name:this.customerForm.name}};}},attachMasterUpdateMetadata(entity,payload,options){{payload.expected_version=options.expected_version;return payload;}},
  loadCustomers:async()=>false,loadCustomerOptions:async()=>true,productEditReturnContext:null,
  closeModal(){{closed+=1;this.modal=null;}},showToast(message,error=false){{notices.push([message,error]);}},errorMessage(error){{return error?.message||String(error);}},handleMaster409(){{return false;}}
}};
vm.handleMasterSaveRefreshFailure=new Function("entity","error",{json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveModal();vm.customerForm={{id:99,version:1,name:"客户B"}};const duplicate=await vm.saveModal();
  expect(duplicate===false&&requests.length===1&&requests[0].url.endsWith("/7"),"customer target changed or duplicate write issued");
  expect(requests[0].payload.expected_version===4&&requests[0].payload.name==="客户A","customer payload/version was not frozen");
  releaseWrite({{data:{{id:7}}}});expect(await first===true,"refresh failure changed successful customer write to failure");
  expect(closed===1,"saved customer editor remained open");
  expect(notices.some(([message,error])=>message.includes("客户已保存")&&message.includes("业务已成功")&&message.includes("请勿重复提交")&&message.includes("手动刷新核对")&&error===true),"customer refresh warning missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "customer-save-refresh.js")

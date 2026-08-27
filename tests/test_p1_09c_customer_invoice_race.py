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


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the customer invoice regression"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_customer_invoice_section_has_independent_loading_retry_and_save_locks() -> None:
    section = INDEX[
        INDEX.index("<section v-if=\"customerForm.id && canManageInvoiceProfiles\"") :
        INDEX.index("普通联系人地址、电话不会自动带入开票资料")
    ]
    assert "customerInvoiceState" in INDEX
    for marker in (
        "税务资料读取中",
        "重新读取税务资料",
        "税务资料保存中…",
        "默认规则读取中",
        "重新读取默认规则",
        "默认规则保存中…",
    ):
        assert marker in section
    assert "profileLoadedCustomerId" in section
    assert "ruleLoadedCustomerId" in section


def test_customer_invoice_profile_and_rule_ignore_cross_customer_responses(
    tmp_path: Path,
) -> None:
    profile_body = _method_body(
        "async loadCustomerInvoiceProfile(customerId=this.customerForm?.id) {",
        "async saveCustomerInvoiceProfile() {",
    )
    rule_body = _method_body(
        "async loadCustomerInvoiceRule(customerId=this.customerForm?.id) {",
        "async saveCustomerInvoiceRule() {",
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.latestRequestControllers = new Map();
const pending = [];
globalThis.axios = {{ get(url, options) {{ return new Promise((resolve, reject) => pending.push({{url,options,resolve,reject}})); }} }};
const vm = {{
  canManageInvoiceProfiles:true,
  customerForm:{{id:1}},
  customerInvoiceProfile:{{invoice_title:"",version:null,error:""}},
  customerInvoiceRule:{{project_name:"",version:null,error:""}},
  customerInvoiceState:{{profileLoading:false,profileSaving:false,profileLoadedCustomerId:null,ruleLoading:false,ruleSaving:false,ruleLoadedCustomerId:null}},
  beginLatestRequest(key) {{ latestRequestControllers.get(key)?.abort(); const controller=new AbortController(); latestRequestControllers.set(key,controller); return controller; }},
  finishLatestRequest(key, controller) {{ if(latestRequestControllers.get(key)===controller) latestRequestControllers.delete(key); }},
  isCancelledRequest(error) {{ return error?.name==="AbortError" || error?.code==="ERR_CANCELED"; }},
  applyCustomerPriceTaxMode() {{}},
  errorMessage(error) {{ return error?.message || String(error); }}
}};
vm.loadCustomerInvoiceProfile = new AsyncFunction("customerId", {json.dumps(profile_body, ensure_ascii=False)}).bind(vm);
vm.loadCustomerInvoiceRule = new AsyncFunction("customerId", {json.dumps(rule_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const profileA=vm.loadCustomerInvoiceProfile(1);
  vm.customerForm.id=2;
  const profileB=vm.loadCustomerInvoiceProfile(2);
  pending[1].resolve({{data:{{invoice_title:"客户B",version:8}}}});
  await profileB;
  pending[0].resolve({{data:{{invoice_title:"客户A",version:3}}}});
  await profileA;
  if(vm.customerInvoiceProfile.invoice_title!=="客户B" || vm.customerInvoiceState.profileLoadedCustomerId!==2) throw new Error("old profile overwrote customer B");

  vm.customerForm.id=1;
  const ruleA=vm.loadCustomerInvoiceRule(1);
  vm.customerForm.id=2;
  const ruleB=vm.loadCustomerInvoiceRule(2);
  pending[3].resolve({{data:{{project_name:"客户B规则",version:9}}}});
  await ruleB;
  pending[2].reject(new Error("客户A旧错误"));
  await ruleA;
  if(vm.customerInvoiceRule.project_name!=="客户B规则" || vm.customerInvoiceState.ruleLoadedCustomerId!==2) throw new Error("old rule error changed customer B");
  if(vm.customerInvoiceRule.error) throw new Error("old customer error leaked into current rule");

  const failed=vm.loadCustomerInvoiceProfile(2);
  pending[4].reject(new Error("网络断开"));
  const failedOk=await failed;
  if(failedOk!==false || !vm.customerInvoiceProfile.error.includes("网络断开")) throw new Error("current profile failure was not shown");
  if(vm.customerInvoiceState.profileLoadedCustomerId!==null || vm.customerInvoiceState.profileLoading) throw new Error("failed profile remained saveable");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "customer-invoice-load-race.js")


def test_customer_invoice_saves_are_single_flight_and_freeze_customer_version(
    tmp_path: Path,
) -> None:
    profile_body = _method_body(
        "async saveCustomerInvoiceProfile() {", "async loadCustomerInvoiceRule("
    )
    rule_body = _method_body(
        "async saveCustomerInvoiceRule() {", "async clearFinanceDashboardFilter()"
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const writes=[];
globalThis.axios={{ put(url,payload){{ return new Promise((resolve,reject)=>writes.push({{url,payload,resolve,reject}})); }} }};
const notices=[];
const vm={{
  canManageInvoiceProfiles:true,
  customerForm:{{id:11}},
  customerInvoiceProfile:{{invoice_title:"客户11",tax_no:"T11",invoice_address:"地址",invoice_phone:"电话",bank_name:"银行",bank_account:"账号",default_seller_id:1,price_tax_mode:"tax_inclusive",default_tax_rate:"0.13",is_enabled:true,confirmation_status:"confirmed",version:4,error:""}},
  customerInvoiceRule:{{project_name:"纸箱",tax_classification_code:"1060105010000000000",unit:"PCS",tax_rate:"0.13",spec_source:"product_code_snapshot",fill_unit_price:true,confirmation_status:"confirmed",version:6,error:""}},
  customerInvoiceState:{{profileLoading:false,profileSaving:false,profileLoadedCustomerId:11,ruleLoading:false,ruleSaving:false,ruleLoadedCustomerId:11}},
  async loadCustomerInvoiceProfile(){{return true;}},
  async loadCustomerInvoiceRule(){{return true;}},
  showToast(message,error){{notices.push([message,error]);}},
  errorMessage(error){{return error?.message||String(error);}}
}};
vm.saveCustomerInvoiceProfile=new AsyncFunction({json.dumps(profile_body, ensure_ascii=False)}).bind(vm);
vm.saveCustomerInvoiceRule=new AsyncFunction({json.dumps(rule_body, ensure_ascii=False)}).bind(vm);

(async()=>{{
  const first=vm.saveCustomerInvoiceProfile();
  const duplicate=vm.saveCustomerInvoiceProfile();
  if(writes.length!==1) throw new Error("profile duplicate click issued multiple PUTs");
  if(!writes[0].url.includes("/customers/11/") || writes[0].payload.expected_version!==4) throw new Error("profile did not freeze customer/version");
  vm.customerForm.id=12;
  writes[0].resolve({{data:{{}}}});
  await Promise.all([first,duplicate]);
  if(vm.customerInvoiceState.profileSaving) throw new Error("profile save lock was not released");
  if(!notices.some(([message])=>message.includes("原客户")&&message.includes("已保存"))) throw new Error("customer switch after profile save was not explained");

  vm.customerForm.id=11;
  vm.customerInvoiceState.ruleLoadedCustomerId=11;
  const ruleFirst=vm.saveCustomerInvoiceRule();
  const ruleDuplicate=vm.saveCustomerInvoiceRule();
  if(writes.length!==2) throw new Error("rule duplicate click issued multiple PUTs");
  if(!writes[1].url.includes("/customers/11/") || writes[1].payload.expected_version!==6) throw new Error("rule did not freeze customer/version");
  writes[1].resolve({{data:{{}}}});
  await Promise.all([ruleFirst,ruleDuplicate]);
  if(vm.customerInvoiceState.ruleSaving) throw new Error("rule save lock was not released");

  vm.customerInvoiceState.profileLoadedCustomerId=11;
  vm.loadCustomerInvoiceProfile=async()=>false;
  const refreshFailure=vm.saveCustomerInvoiceProfile();
  writes[2].resolve({{data:{{}}}});
  await refreshFailure;
  if(!notices.some(([message,error])=>message.includes("资料已保存")&&message.includes("不要重复")&&error===true)) throw new Error("profile refresh failure did not prevent repeat save");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "customer-invoice-save-lock.js")


def test_customer_invoice_frontend_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "customer-invoice-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

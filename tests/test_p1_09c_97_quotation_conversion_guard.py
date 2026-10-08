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


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for quotation conversion regression"
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


def test_conversion_ui_has_dedicated_saving_committed_and_cancel_guards() -> None:
    assert (
        'quotationConversionState:{saving:false,committed:false,outcomeUncertain:false,'
        'itemId:null,productId:null}'
    ) in INDEX
    assert "quotationConversionState.saving ? '正在转入…'" in INDEX
    assert "quotationConversionState.committed ? '已转入常用箱'" in INDEX
    assert "modal?.type==='quotationConvert' && quotationConversionState.saving" in INDEX
    assert "报价明细正在转入常用箱，请勿重复点击" in INDEX
    assert "该报价明细已经转入常用箱，请刷新列表核对" in INDEX


def test_conversion_runtime_blocks_duplicate_and_freezes_item_and_payload(tmp_path: Path) -> None:
    body = _method_body("async saveQuotationConversion() {", "bomComponentInternalCode(component) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
let release,postCount=0,closeCount=0;
const calls=[];
const axios={{post:(url,payload)=>{{postCount+=1;calls.push({{url,payload}});return new Promise(resolve=>{{release=()=>resolve({{data:{{product_id:88}}}});}});}}}};
const factory=new Function("axios","return async function() {{"+body+"}}");
const sourcePayload={{product_code:"P-007",nested:{{value:1}}}};
const vm={{
  quotationConversionState:{{saving:false,committed:false,outcomeUncertain:false,itemId:null,productId:null}},
  quotationConvertForm:{{quotation_item_id:7}},
  quotationConvertPayload(){{return sourcePayload;}},
  closeModal(){{closeCount+=1;}},
  loadQuotationHistory:async()=>true,
  loadProducts:async()=>true,
  showToast(){{}},errorMessage:error=>error.message,
}};
vm.saveQuotationConversion=factory(axios).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveQuotationConversion();
  const duplicate=await Promise.race([
    vm.saveQuotationConversion(),
    new Promise((_,reject)=>setTimeout(()=>reject(new Error("duplicate call stayed pending")),50)),
  ]);
  vm.quotationConvertForm.quotation_item_id=99;
  sourcePayload.nested.value=9;
  expect(duplicate?._in_flight===true,"duplicate conversion was not rejected");
  expect(postCount===1,"duplicate conversion sent another request");
  expect(calls[0].url==="/api/quotations/items/7/convert-to-product","quotation item target was not frozen");
  expect(calls[0].payload.product_code==="P-007"&&calls[0].payload.nested.value===1,"conversion payload was not frozen");
  release();const result=await first;
  expect(result.product_id===88&&vm.quotationConversionState.committed,"successful conversion was not committed locally");
  expect(closeCount===1,"successful conversion did not close original modal");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    command = _method_body("quotationCommand(owner, action, body) {", "quotationCommandFailed(owner, action, pending, error) {")
    script = script.replace("quotationConversionState:", "quotationCommand: new Function('owner','action','body'," + json.dumps(command) + "), quotationConversionState:")
    _run_node(script, tmp_path, "quotation-conversion-duplicate.js")


def test_conversion_refresh_failure_preserves_committed_success(tmp_path: Path) -> None:
    body = _method_body("async saveQuotationConversion() {", "bomComponentInternalCode(component) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const axios={{post:async()=>({{data:{{product_id:88}}}})}};
const factory=new Function("axios","return async function() {{"+body+"}}");
const toasts=[];let closed=false;
const vm={{
  quotationConversionState:{{saving:false,committed:false,outcomeUncertain:false,itemId:null,productId:null}},
  quotationConvertForm:{{quotation_item_id:7}},quotationConvertPayload:()=>({{product_code:"P-007"}}),
  closeModal(){{closed=true;}},loadQuotationHistory:async()=>{{throw new Error("history offline");}},
  loadProducts:async()=>true,showToast:(message,isError)=>toasts.push({{message,isError}}),errorMessage:error=>error.message,
}};
(async()=>{{
  const result=await factory(axios).bind(vm)();
  if(!closed||!vm.quotationConversionState.committed)throw new Error("committed conversion was not locked and closed");
  if(!result._refresh_failed)throw new Error("refresh failure was not returned");
  if(toasts.length!==1||!toasts[0].message.includes("常用箱已创建")||!toasts[0].message.includes("不要重复提交"))throw new Error("partial success truth was hidden");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    command = _method_body("quotationCommand(owner, action, body) {", "quotationCommandFailed(owner, action, pending, error) {")
    script = script.replace("quotationConversionState:", "quotationCommand: new Function('owner','action','body'," + json.dumps(command) + "), quotationConversionState:")
    _run_node(script, tmp_path, "quotation-conversion-refresh.js")


def test_conversion_network_uncertain_closes_modal_but_explicit_failure_can_retry(tmp_path: Path) -> None:
    body = _method_body("async saveQuotationConversion() {", "bomComponentInternalCode(component) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=axios=>new Function("axios","return async function() {{"+body+"}}")(axios);
const makeVm=()=>({{
  quotationConversionState:{{saving:false,committed:false,outcomeUncertain:false,itemId:null,productId:null}},
  quotationConvertForm:{{quotation_item_id:7}},quotationConvertPayload:()=>({{product_code:"P-007"}}),
  closeCount:0,closeModal(){{this.closeCount+=1;}},loadQuotationHistory:async()=>true,loadProducts:async()=>true,
  showToast(){{}},errorMessage:error=>error.message,
}});
(async()=>{{
  const networkVm=makeVm();
  const networkError=new Error("network");
  try{{await factory({{post:async()=>{{throw networkError;}}}}).bind(networkVm)();throw new Error("network failure did not throw");}}
  catch(error){{
    if(error!==networkError||!error._quotationConversionOutcomeUncertain)throw error;
    if(networkVm.closeCount!==1||!networkVm.quotationConversionState.outcomeUncertain)throw new Error("uncertain conversion kept original modal active");
  }}
  const explicitVm=makeVm();
  const explicitError=new Error("duplicate");explicitError.response={{status:409}};
  try{{await factory({{post:async()=>{{throw explicitError;}}}}).bind(explicitVm)();throw new Error("explicit failure did not throw");}}
  catch(error){{
    if(error!==explicitError)throw error;
    if(explicitVm.closeCount!==0||explicitVm.quotationConversionState.saving||explicitVm.quotationConversionState.committed||explicitVm.quotationConversionState.outcomeUncertain)throw new Error("explicit failure did not allow correction and retry");
  }}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    command = _method_body("quotationCommand(owner, action, body) {", "quotationCommandFailed(owner, action, pending, error) {")
    script = script.replace("quotationConversionState:", "quotationCommand: new Function('owner','action','body'," + json.dumps(command) + "), quotationConversionState:")
    _run_node(script, tmp_path, "quotation-conversion-errors.js")


def test_save_modal_handles_committed_and_uncertain_outcomes_without_generic_success() -> None:
    body = _method_body("async saveModal() {", "async dispatchDelivery(row, options = {}) {")
    assert "if (this.modal?.type === \"quotationConvert\"" in body
    assert "const conversion = await this.saveQuotationConversion();" in body
    assert "if (conversion?._in_flight) return false;" in body
    assert "if (error?._quotationConversionOutcomeUncertain)" in body
    assert "结果暂不确定" in body
    assert "刷新客户报价和常用箱列表核对" in body

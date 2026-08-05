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
    assert node is not None, "Node.js is required for the quotation action regression"
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


def test_quotation_actions_have_local_feedback_and_disable_related_writes() -> None:
    assert 'quotationAction:{action:"",quotationId:null,quotationNo:""}' in INDEX
    assert 'quotationDraftAction:""' in INDEX
    assert "quotationDraftAction === 'save' ? '保存中…'" in INDEX
    assert "quotationDraftAction === 'generate' ? '生成中…'" in INDEX
    assert "quotationActionPending('accept',row.id) ? '接受中…'" in INDEX
    assert "quotationActionPending('void',row.id) ? '作废中…'" in INDEX
    assert ':disabled="loading || quotationActionBusy()' in INDEX
    assert "if (this.loading || this.quotationActionBusy()) return false;" in _method_body(
        "async convertQuotationItem(quotation,item) {",
        "markQuotationConvertManual() {",
    )


def test_quotation_action_runtime_blocks_duplicates_cross_actions_and_failure(tmp_path: Path) -> None:
    body = _method_body(
        "async runQuotationAction({action, quotationId=null, quotationNo=\"\", confirmMessage, task}) {",
        "async refreshQuotationHistoryAfterMutation(successMessage) {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function('return async function({{action, quotationId=null, quotationNo="", confirmMessage, task}}) {{'+body+'}}');
let confirmCount=0,taskCount=0,toastCount=0,release,allowConfirm=true;
global.confirm=()=>{{confirmCount+=1;return allowConfirm;}};
const vm={{
  loading:false,
  quotationAction:{{action:"",quotationId:null,quotationNo:""}},
  quotationActionBusy(){{return Boolean(this.quotationAction?.action);}},
  showToast(){{toastCount+=1;}},
  errorMessage(error){{return error?.message||String(error);}},
}};
vm.runQuotationAction=factory().bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.runQuotationAction({{action:"accept",quotationId:7,quotationNo:"QT-007",confirmMessage:"确认",task:()=>{{taskCount+=1;return new Promise(resolve=>{{release=resolve;}});}}}});
  const duplicate=await vm.runQuotationAction({{action:"accept",quotationId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  const crossed=await vm.runQuotationAction({{action:"void",quotationId:8,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(duplicate===false&&crossed===false,"duplicate or cross action was not rejected");
  expect(confirmCount===1&&taskCount===1,"blocked action repeated confirmation or request");
  release(true);expect(await first===true,"first quotation action did not complete");
  expect(vm.quotationAction.action==="","success did not release quotation action lock");
  const failed=await vm.runQuotationAction({{action:"void",quotationId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;throw new Error("失败");}}}});
  expect(failed===false&&toastCount===1,"failure was not reported once");
  expect(vm.quotationAction.action==="","failure did not release quotation action lock");
  allowConfirm=false;
  const cancelled=await vm.runQuotationAction({{action:"void",quotationId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(cancelled===false&&taskCount===2,"cancelled confirmation still executed mutation");
  vm.loading=true;
  const whileSaving=await vm.runQuotationAction({{action:"accept",quotationId:7,task:async()=>{{taskCount+=1;return true;}}}});
  expect(whileSaving===false&&taskCount===2,"quotation action crossed an active draft save");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "quotation-action-runtime.js")


def test_accept_and_void_freeze_targets_and_preserve_committed_truth() -> None:
    accept_body = _method_body("async acceptQuotation(row) {", "async voidQuotation(row) {")
    void_body = _method_body("async voidQuotation(row) {", "async convertQuotationItem(quotation,item) {")
    refresh_body = _method_body(
        "async refreshQuotationHistoryAfterMutation(successMessage) {",
        "async acceptQuotation(row) {",
    )

    for action, body in (("accept", accept_body), ("void", void_body)):
        assert "const target = {" in body
        assert "this.runQuotationAction({" in body
        assert f'action:"{action}"' in body
        assert "quotationId:target.id" in body
        assert "target.quotationNo" in body
        assert "await this.refreshQuotationHistoryAfterMutation(" in body
    assert "不要重复操作" in refresh_body
    assert "请手动刷新核对" in refresh_body


def test_quotation_refresh_failure_reports_partial_success(tmp_path: Path) -> None:
    body = _method_body(
        "async refreshQuotationHistoryAfterMutation(successMessage) {",
        "async acceptQuotation(row) {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function(successMessage) {{"+body+"}}");
const toasts=[];
const vm={{
  loadQuotationHistory:async()=>{{throw new Error("offline");}},
  showToast:(message,isError)=>toasts.push({{message,isError}}),
  errorMessage:error=>error.message,
}};
(async()=>{{
  const result=await factory().bind(vm)("报价单已作废");
  if(result!==false)throw new Error("refresh failure should be reported to caller");
  if(toasts.length!==1||!toasts[0].isError)throw new Error("refresh failure was not shown once");
  if(!toasts[0].message.includes("报价单已作废")||!toasts[0].message.includes("不要重复操作"))throw new Error("partial success message hid committed action");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "quotation-refresh-partial-success.js")


def test_generate_opens_print_before_refresh_and_keeps_generation_truth() -> None:
    persist_body = _method_body("async persistQuotation(showMessage=true, refreshHistory=true) {", "async saveQuotationDraft() {")
    generate_body = _method_body("async generateQuotation() {", "editQuotation(row) {")
    assert "refreshQuotationHistoryAfterMutation" in persist_body
    assert "await this.persistQuotation(false, false)" in generate_body
    assert generate_body.index("window.open") < generate_body.index("refreshQuotationHistoryAfterMutation")
    assert "报价单 ${data.quotation_no} 已生成" in generate_body

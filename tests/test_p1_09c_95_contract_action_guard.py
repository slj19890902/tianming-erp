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
    assert node is not None, "Node.js is required for the contract action regression"
    target = tmp_path / "contract-action-guard.js"
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


def test_contract_actions_have_local_feedback_and_disable_related_writes() -> None:
    assert 'contractAction:{action:"",contractId:null}' in INDEX
    assert "contractActionPending('confirm',contractDraft.id) ? '确认中…'" in INDEX
    assert "contractActionPending('convert',contractDraft.id) ? '转单中…'" in INDEX
    assert "contractActionPending('delete',contractDraft.id) ? '删除中…'" in INDEX
    assert "contractActionPending('confirm',row.id) ? '确认中…'" in INDEX
    assert "contractActionPending('convert',row.id) ? '转单中…'" in INDEX
    assert "contractActionPending('delete',row.id) ? '删除中…'" in INDEX
    assert "!this.contractActionBusy()" in _method_body("contractEditable() {", "contractProductOptions() {")


def test_contract_action_runtime_blocks_duplicates_cancel_and_failure(tmp_path: Path) -> None:
    body = _method_body(
        "async runContractAction({action, contractId=null, confirmMessage, task}) {",
        "async refreshContractHistoryAfterMutation(successMessage) {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function({{action, contractId=null, confirmMessage, task}}) {{"+body+"}}");
let confirmCount=0,taskCount=0,toastCount=0,allowConfirm=true,release;
global.confirm=()=>{{confirmCount+=1;return allowConfirm;}};
const vm={{
  loading:false,
  contractAction:{{action:"",contractId:null}},
  contractActionBusy(){{return Boolean(this.contractAction?.action);}},
  showToast(){{toastCount+=1;}},
  errorMessage(error){{return error?.message||String(error);}}
}};
vm.runContractAction=factory().bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.runContractAction({{action:"convert",contractId:7,confirmMessage:"确认",task:()=>{{taskCount+=1;return new Promise(resolve=>{{release=resolve;}});}}}});
  const duplicate=await vm.runContractAction({{action:"convert",contractId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(duplicate===false,"duplicate contract action was not rejected");
  expect(confirmCount===1&&taskCount===1,"duplicate action repeated confirmation or request");
  release(true);expect(await first===true,"first contract action did not complete");
  expect(vm.contractAction.action==="","success did not release contract action lock");
  const failed=await vm.runContractAction({{action:"confirm",contractId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;throw new Error("失败");}}}});
  expect(failed===false&&toastCount===1,"failure was not reported once");
  expect(vm.contractAction.action==="","failure did not release contract action lock");
  allowConfirm=false;
  const cancelled=await vm.runContractAction({{action:"delete",contractId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(cancelled===false&&taskCount===2,"cancelled confirmation still executed mutation");
  expect(vm.contractAction.action==="","cancelled confirmation acquired contract action lock");
  vm.loading=true;
  const whileSaving=await vm.runContractAction({{action:"confirm",contractId:7,task:async()=>{{taskCount+=1;return true;}}}});
  expect(whileSaving===false&&taskCount===2,"contract lifecycle action crossed an active draft save");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_contract_mutations_freeze_targets_and_report_refresh_partial_success() -> None:
    delete_body = _method_body("async deleteContract(row) {", "async confirmContract(row) {")
    confirm_body = _method_body("async confirmContract(row) {", "async convertContract(row) {")
    convert_body = _method_body("async convertContract(row) {", "printContract(row) {")
    refresh_body = _method_body(
        "async refreshContractHistoryAfterMutation(successMessage) {",
        "async deleteContract(row) {",
    )

    for action, body in (("delete", delete_body), ("confirm", confirm_body), ("convert", convert_body)):
        assert "const target = {" in body
        assert "this.runContractAction({" in body
        assert f'action:"{action}"' in body
        assert "contractId:target.id" in body
        assert "target.contractNo" in body
        assert "await this.refreshContractHistoryAfterMutation(" in body
    assert "target.version" in confirm_body
    assert "target.version" in convert_body
    assert "target.version" in delete_body
    assert "expected_version:target.version" in delete_body
    assert "target.idempotencyKey" in convert_body
    assert "不要重复操作" in refresh_body
    assert "请手动刷新核对" in refresh_body


def test_convert_contract_runtime_uses_frozen_target_and_one_idempotency_key(tmp_path: Path) -> None:
    body = _method_body("async convertContract(row) {", "printContract(row) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const calls=[],messages=[];
const axios={{post:async(url,payload)=>{{calls.push({{url,payload}});return {{data:{{id:7,status:"converted",converted_order_number:"TM-001"}}}};}}}};
const factory=new Function("axios","return async function(row) {{"+body+"}}");
let keyCount=0;
const vm={{
  contractIdempotencyKey(){{keyCount+=1;return "frozen-key";}},
  runContractAction({{task}}){{return task();}},
  hydrateContract(data){{return data;}},
  refreshContractHistoryAfterMutation(message){{messages.push(message);return Promise.resolve(false);}},
  contractDraft:null,
}};
const convert=factory(axios).bind(vm);
const row={{id:7,version:3,contract_no:"CT-007"}};
(async()=>{{
  const pending=convert(row);
  row.id=99;row.version=88;row.contract_no="CHANGED";
  const result=await pending;
  if(result!==true)throw new Error("convert did not preserve committed success");
  if(keyCount!==1||calls.length!==1)throw new Error("convert generated multiple keys or requests");
  if(calls[0].url!=="/api/contracts/7/convert-order")throw new Error("contract id was not frozen");
  if(calls[0].payload.expected_version!==3||calls[0].payload.idempotency_key!=="frozen-key")throw new Error("version or idempotency key was not frozen");
  if(!messages[0].includes("TM-001"))throw new Error("committed conversion message was lost");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_delete_contract_runtime_freezes_version_and_preserves_draft_on_conflict(tmp_path: Path) -> None:
    body = _method_body("async deleteContract(row) {", "async confirmContract(row) {")
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const calls=[],toasts=[];
let rejectDelete;
const axios={{delete:(url,options)=>{{calls.push({{url,options}});return new Promise((resolve,reject)=>{{rejectDelete=reject;}});}}}};
const factory=new Function("axios","return async function(row) {{"+body+"}}");
const vm={{
  contractDraft:{{id:7,version:3,remarks:"保留当前编辑内容"}},
  runContractAction:async({{task}})=>{{try{{return await task();}}catch(error){{vm.showToast(vm.errorMessage(error),true);return false;}}}},
  newContractDraft(){{throw new Error("conflict cleared the current draft");}},
  refreshContractHistoryAfterMutation(){{throw new Error("conflict refreshed after an uncommitted delete");}},
  showToast:(message,isError)=>toasts.push({{message,isError}}),
  errorMessage:error=>error.response.data.detail,
}};
const row={{id:7,version:3,contract_no:"CT-007"}};
(async()=>{{
  const pending=factory(axios).bind(vm)(row);
  row.id=99;row.version=88;row.contract_no="CHANGED";
  rejectDelete({{response:{{status:409,data:{{detail:"合同已被其他操作更新，请刷新后重新确认删除"}}}}}});
  const result=await pending;
  if(result!==false)throw new Error("delete conflict was reported as success");
  if(calls.length!==1||calls[0].url!=="/api/contracts/7")throw new Error("contract id was not frozen");
  if(calls[0].options.data.expected_version!==3)throw new Error("contract version was not frozen");
  if(vm.contractDraft.remarks!=="保留当前编辑内容")throw new Error("draft content changed after conflict");
  if(toasts.length!==1||!toasts[0].isError||!toasts[0].message.includes("刷新"))throw new Error("conflict refresh guidance was not shown");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_contract_refresh_failure_reports_committed_truth(tmp_path: Path) -> None:
    body = _method_body(
        "async refreshContractHistoryAfterMutation(successMessage) {",
        "async deleteContract(row) {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function(successMessage) {{"+body+"}}");
const toasts=[];
const vm={{
  loadContractHistory:async()=>{{throw new Error("offline");}},
  showToast:(message,isError)=>toasts.push({{message,isError}}),
  errorMessage:error=>error.message,
}};
(async()=>{{
  const result=await factory().bind(vm)("合同已确认并锁定");
  if(result!==false)throw new Error("refresh failure should be reported to the caller");
  if(toasts.length!==1||!toasts[0].isError)throw new Error("refresh failure was not shown once");
  if(!toasts[0].message.includes("合同已确认并锁定")||!toasts[0].message.includes("不要重复操作"))throw new Error("partial success message hid the committed action");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)

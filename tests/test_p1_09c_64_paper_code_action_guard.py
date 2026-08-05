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
    assert node is not None, "Node.js is required for the paper-code regression"
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


def test_paper_code_buttons_use_local_action_state_and_truthful_feedback() -> None:
    assert 'paperCodeAction:{action:"",paperCodeId:null}' in INDEX
    assert ':disabled="paperCodeActionBusy()" @click="savePaperCode"' in INDEX
    assert "paperCodeActionPending('save') ? '保存中…'" in INDEX
    assert ':disabled="paperCodeActionBusy()" @click="togglePaperCodeStatus(row)"' in INDEX
    assert "paperCodeActionPending('status',row.id) ? '处理中…'" in INDEX
    assert 'savePaperCode">{{ paperCodeForm.id ? \'保存修改\' : \'新增代码\' }}</button>' not in INDEX


def test_paper_code_action_runtime_blocks_duplicates_and_releases(tmp_path: Path) -> None:
    body = _method_body(
        "async runPaperCodeAction({action, paperCodeId=null, task}) {",
        "async savePaperCode() {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function({{action, paperCodeId=null, task}}) {{"+body+"}}");
let taskCount=0,toastCount=0,release;
const vm={{
  paperCodeAction:{{action:"",paperCodeId:null}},
  paperCodeActionBusy(){{return Boolean(this.paperCodeAction?.action);}},
  showToast(){{toastCount+=1;}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.runPaperCodeAction=factory().bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.runPaperCodeAction({{action:"save",paperCodeId:5,task:()=>{{taskCount+=1;return new Promise(resolve=>{{release=resolve;}});}}}});
  const duplicate=await vm.runPaperCodeAction({{action:"status",paperCodeId:6,task:async()=>{{taskCount+=1;return true;}}}});
  expect(duplicate===false,"duplicate paper-code action was not rejected");
  expect(taskCount===1,"duplicate paper-code action issued another mutation");
  release(true);expect(await first===true,"first paper-code action did not complete");
  expect(vm.paperCodeAction.action==="","success did not release paper-code lock");
  const failed=await vm.runPaperCodeAction({{action:"save",paperCodeId:5,task:async()=>{{taskCount+=1;throw new Error("失败");}}}});
  expect(failed===false&&toastCount===1,"failure was not reported once");
  expect(vm.paperCodeAction.action==="","failure did not release paper-code lock");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "paper-code-action-guard.js")


def test_save_paper_code_freezes_target_and_reports_refresh_failure(tmp_path: Path) -> None:
    body = _method_body("async savePaperCode() {", "async togglePaperCodeStatus(row) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,resetCount=0;const notices=[];let release;
global.axios={{put:(url,payload)=>new Promise(resolve=>{{writes+=1;release=()=>resolve({{data:{{id:7}}}});global.saved={{url,payload}};}})}};
const vm={{
  paperCodeForm:{{id:7,supplier_name:"苏州嘉林亿",code_char:"+",paper_name:"加强纸",gram_weight:140,is_active:true}},
  paperCodeSupplierFilter:"",paperCodeAction:{{action:"",paperCodeId:null}},
  paperCodePayload(row){{return {{...row}};}},
  runPaperCodeAction:async function(options){{if(this.paperCodeAction.action)return false;this.paperCodeAction={{action:options.action,paperCodeId:options.paperCodeId}};try{{return (await options.task())!==false;}}catch(error){{this.showToast(this.errorMessage(error),true);return false;}}finally{{this.paperCodeAction={{action:"",paperCodeId:null}};}}}},
  loadPaperCodes:async()=>false,resetPaperCodeForm(){{resetCount+=1;}},
  showToast(message,error=false){{notices.push([message,error]);}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.savePaperCode=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const pending=vm.savePaperCode();
  vm.paperCodeForm={{id:99,supplier_name:"其他",code_char:"Z",paper_name:"被改动",gram_weight:1,is_active:false}};
  release();
  expect(await pending===true,"successful write plus refresh failure was reported as mutation failure");
  expect(writes===1&&global.saved.url.endsWith("/7"),"save did not freeze the clicked record id");
  expect(global.saved.payload.code_char==="+"&&global.saved.payload.paper_name==="加强纸","save did not freeze the clicked payload");
  expect(resetCount===1,"saved form was not cleared after committed write");
  expect(notices.some(([message,error])=>message.includes("已保存")&&message.includes("列表刷新失败")&&message.includes("不要重复保存")&&error===true),"partial success guidance missing");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "paper-code-save-refresh.js")


def test_toggle_paper_code_status_freezes_direction_and_uses_shared_guard(tmp_path: Path) -> None:
    body = _method_body("async togglePaperCodeStatus(row) {", "toggleMaterialComposer() {")
    assert "this.runPaperCodeAction({" in body
    assert 'action:"status"' in body
    assert "const desiredActive = !wasActive;" in body
    assert "is_active:desiredActive" in body
    assert "const refreshed = await this.loadPaperCodes();" in body
    assert "状态已更新，但列表刷新失败" in body
    assert "wasActive ? \"基础代码已停用\" : \"基础代码已启用\"" in body
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,release;const notices=[];
global.axios={{put:(url,payload)=>new Promise(resolve=>{{writes+=1;global.saved={{url,payload}};release=resolve;}})}};
const vm={{
  paperCodeAction:{{action:"",paperCodeId:null}},paperCodePayload(row){{return {{...row}};}},
  runPaperCodeAction:async function(options){{if(this.paperCodeAction.action)return false;this.paperCodeAction={{action:options.action,paperCodeId:options.paperCodeId}};try{{return (await options.task())!==false;}}catch(error){{this.showToast(this.errorMessage(error),true);return false;}}finally{{this.paperCodeAction={{action:"",paperCodeId:null}};}}}},
  loadPaperCodes:async()=>true,showToast(message,error=false){{notices.push([message,error]);}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.togglePaperCodeStatus=new AsyncFunction("row",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const row={{id:8,supplier_name:"苏州嘉林亿",code_char:"+",paper_name:"加强纸",gram_weight:140,is_active:true}};
  const first=vm.togglePaperCodeStatus(row);
  row.id=99;row.is_active=false;
  const duplicate=await vm.togglePaperCodeStatus(row);
  expect(duplicate===false&&writes===1,"double status click issued another mutation");
  expect(global.saved.url.endsWith("/8")&&global.saved.payload.is_active===false,"status mutation did not freeze id/direction");
  release({{data:{{id:8}}}});expect(await first===true,"first status mutation did not finish");
  expect(notices.some(([message])=>message==="基础代码已停用"),"success feedback used mutated row state");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "paper-code-status-guard.js")


def test_load_paper_codes_returns_latest_request_result() -> None:
    body = _method_body("async loadPaperCodes() {", "resetPaperCodeForm() {")
    assert 'const requestKey = "materials:paper-codes";' in body
    assert "this.beginLatestRequest(requestKey)" in body
    assert "latestRequestControllers.get(requestKey) !== controller) return false" in body
    assert "this.paperCodes = data.items || [];" in body
    assert "return true;" in body
    assert "return false;" in body

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
    assert node is not None, "Node.js is required for the flute-rule regression"
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


def test_flute_rule_buttons_use_local_action_state() -> None:
    assert 'fluteRuleAction:{action:"",ruleId:null}' in INDEX
    assert ':disabled="fluteRuleActionBusy()" @click="saveFluteRule()"' in INDEX
    assert "fluteRuleActionPending('save') ? '保存中…'" in INDEX
    assert ':disabled="fluteRuleActionBusy()" @click="disableFluteRule(r)"' in INDEX
    assert "fluteRuleActionPending('disable',r.id) ? '处理中…'" in INDEX


def test_flute_rule_action_runtime_blocks_duplicates_and_releases(tmp_path: Path) -> None:
    body = _method_body(
        "async runFluteRuleAction({action, ruleId=null, task}) {",
        "async saveFluteRule() {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function({{action, ruleId=null, task}}) {{"+body+"}}");
let taskCount=0,release;
const vm={{
  fluteRuleAction:{{action:"",ruleId:null}},fluteRuleError:"",
  fluteRuleActionBusy(){{return Boolean(this.fluteRuleAction?.action);}},
  errorMessage(error){{return error?.message||String(error);}}
}};
vm.runFluteRuleAction=factory().bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.runFluteRuleAction({{action:"save",ruleId:5,task:()=>{{taskCount+=1;return new Promise(resolve=>{{release=resolve;}});}}}});
  const duplicate=await vm.runFluteRuleAction({{action:"disable",ruleId:6,task:async()=>{{taskCount+=1;return true;}}}});
  expect(duplicate===false,"duplicate flute-rule action was not rejected");
  expect(taskCount===1,"duplicate flute-rule action issued another mutation");
  release(true);expect(await first===true,"first flute-rule action did not complete");
  expect(vm.fluteRuleAction.action==="","success did not release flute-rule lock");
  const failed=await vm.runFluteRuleAction({{action:"save",ruleId:5,task:async()=>{{throw new Error("失败");}}}});
  expect(failed===false&&vm.fluteRuleError==="失败","failure was not reported once");
  expect(vm.fluteRuleAction.action==="","failure did not release flute-rule lock");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "flute-rule-action-guard.js")


def test_save_flute_rule_freezes_payload_and_reports_refresh_failure(tmp_path: Path) -> None:
    body = _method_body("async saveFluteRule() {", "async disableFluteRule(r) {")
    assert "this.runFluteRuleAction({" in body
    assert 'action:"save"' in body
    assert "const snapshot =" in body
    assert "规则已保存，但列表刷新失败" in body
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,release,resetCount=0;const notices=[];
global.axios={{patch:(url,payload)=>new Promise(resolve=>{{writes+=1;global.saved={{url,payload}};release=resolve;}})}};
const vm={{
  fluteRuleForm:{{id:7,supplier_name:"苏州嘉林亿",layer_count:3,flute_type:"A",price_delta:"0.04",effective_date:"2026-08-05",remark:"测试",is_active:true}},
  fluteRuleError:"",fluteRuleAction:{{action:"",ruleId:null}},
  runFluteRuleAction:async function(options){{if(this.fluteRuleAction.action)return false;this.fluteRuleAction={{action:options.action,ruleId:options.ruleId}};try{{return (await options.task())!==false;}}catch(error){{this.fluteRuleError=error?.message||String(error);return false;}}finally{{this.fluteRuleAction={{action:"",ruleId:null}};}}}},
  loadFluteRules:async()=>false,resetFluteRuleForm(){{resetCount+=1;}},
  showToast(message,error=false){{notices.push([message,error]);}}
}};
vm.saveFluteRule=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const pending=vm.saveFluteRule();
  vm.fluteRuleForm={{id:99,supplier_name:"其他",layer_count:5,flute_type:"BE",price_delta:"9",effective_date:"",remark:"被改动",is_active:false}};
  release({{data:{{id:7}}}});expect(await pending===true,"committed write plus refresh failure was reported as mutation failure");
  expect(writes===1&&global.saved.url.endsWith("/7"),"save did not freeze clicked rule id");
  expect(global.saved.payload.supplier_name==="苏州嘉林亿"&&global.saved.payload.price_delta==="0.04","save did not freeze clicked payload");
  expect(resetCount===1,"saved form was not cleared after committed write");
  expect(notices.some(([message,error])=>message.includes("规则已保存")&&message.includes("列表刷新失败")&&message.includes("不要重复操作")&&error===true),"partial success guidance missing");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "flute-rule-save-refresh.js")


def test_disable_flute_rule_freezes_target_and_uses_shared_guard() -> None:
    body = _method_body("async disableFluteRule(r) {", "materialBaseCode(row) {")
    assert "this.runFluteRuleAction({" in body
    assert 'action:"disable"' in body
    assert "const snapshot =" in body
    assert "规则已停用，但列表刷新失败" in body
    assert "snapshot.id" in body


def test_load_flute_rules_accepts_only_latest_request_result() -> None:
    body = _method_body("async loadFluteRules() {", "editFluteRule(r) {")
    assert 'const requestKey = "materials:flute-price-rules";' in body
    assert "this.beginLatestRequest(requestKey)" in body
    assert "signal:controller.signal" in body
    assert "latestRequestControllers.get(requestKey) !== controller) return false" in body
    assert "this.fluteRules = data.rules || data.items || [];" in body
    assert "this.isCancelledRequest(error)" in body
    assert "return true;" in body
    assert "return false;" in body

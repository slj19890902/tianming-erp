from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUOTATION = (ROOT / "static" / "quotation-print.html").read_text(encoding="utf-8")
CONTRACT = (ROOT / "static" / "contract-print.html").read_text(encoding="utf-8")
UTIL_PATH = ROOT / "static" / "assets" / "print-recovery.js"
UTIL = UTIL_PATH.read_text(encoding="utf-8") if UTIL_PATH.exists() else ""


def _inline_script(page: str) -> str:
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", page, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    return scripts[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_both_print_pages_use_shared_loading_and_retry_contract() -> None:
    assert UTIL
    for marker in (
        "function parseErrorDetail",
        "async function requestPrintData",
        "AbortController",
        "loadGeneration",
        "无法连接 ERP 服务",
        "HTTP ${response.status}",
        "请求编号",
    ):
        assert marker in UTIL
    for page, label, loader in (
        (QUOTATION, "正在读取报价单", "loadQuotation"),
        (CONTRACT, "正在读取合同", "loadContract"),
    ):
        assert 'id="printButton" disabled' in page
        assert 'id="retryButton"' in page
        assert 'id="loadState"' in page
        assert 'id="errorBox"' in page
        assert "print-recovery.js" in page
        assert "TmPrintRecovery.createPrintPage" in page
        assert label in page
        assert f"async function {loader}" in page
        assert "document.body.innerHTML" not in page


def test_shared_print_recovery_handles_errors_success_and_stale_response(tmp_path: Path) -> None:
    harness = f"""
const vm=require("vm");global.window=global;
class FakeAbortController{{constructor(){{this.signal={{aborted:false}}}}abort(){{this.signal.aborted=true}}}};global.AbortController=FakeAbortController;
const classList=()=>{{const values=new Set(["hidden"]);return{{add:v=>values.add(v),remove:v=>values.delete(v),toggle:(v,f)=>f?values.add(v):values.delete(v),contains:v=>values.has(v)}}}};
const node=id=>({{id,textContent:"",innerHTML:"",disabled:false,hidden:false,classList:classList(),listeners:{{}},addEventListener(t,f){{this.listeners[t]=f}}}});
const nodes=Object.fromEntries(["printButton","retryButton","loadState","errorBox","content"].map(id=>[id,node(id)]));
vm.runInThisContext({json.dumps(UTIL, ensure_ascii=False)});
const responses=[
  {{kind:"network"}},
  {{ok:false,status:401,text:"",headers:{{get:()=>"REQ-401"}}}},
  {{ok:true,status:200,text:"",headers:{{get:()=>"REQ-EMPTY"}}}},
  {{ok:false,status:500,text:"broken",headers:{{get:()=>"REQ-500"}}}},
  {{ok:false,status:409,text:JSON.stringify({{detail:{{message:"单据状态已变化",code:"DOC_STALE"}}}}),headers:{{get:()=>"REQ-409"}}}},
  {{ok:true,status:200,text:JSON.stringify({{number:"NEW"}}),headers:{{get:()=>null}}}},
];
global.fetch=async()=>{{const next=responses.shift();if(next.kind==="network")throw new TypeError("Failed to fetch");return{{ok:next.ok,status:next.status,headers:next.headers,text:async()=>next.text}}}};
const page=TmPrintRecovery.createPrintPage({{
  id:"7",url:id=>`/api/docs/${{id}}`,printButton:nodes.printButton,retryButton:nodes.retryButton,
  loadState:nodes.loadState,errorBox:nodes.errorBox,content:nodes.content,
  loadingMessage:"正在读取单据…",invalidMessage:"链接无效",authMessage:"登录已失效",
  failureMessage:"读取单据失败",render:data=>{{nodes.content.innerHTML=data.number}},print:()=>{{}},
}});
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  await page.load();expect(nodes.errorBox.textContent.includes("无法连接 ERP"),"network message missing");expect(nodes.printButton.disabled,"network failure unlocked print");
  await nodes.retryButton.listeners.click();expect(nodes.errorBox.textContent.includes("登录已失效"),"401 auth message missing");expect(nodes.errorBox.textContent.includes("HTTP 401"),"401 status missing");expect(nodes.errorBox.textContent.includes("REQ-401"),"401 request id missing");
  await nodes.retryButton.listeners.click();expect(nodes.errorBox.textContent.includes("空白或无法识别"),"empty response message missing");expect(nodes.errorBox.textContent.includes("HTTP 200"),"empty response status missing");expect(nodes.errorBox.textContent.includes("REQ-EMPTY"),"empty response request id missing");
  await nodes.retryButton.listeners.click();expect(nodes.errorBox.textContent.includes("HTTP 500"),"HTTP status missing");expect(nodes.errorBox.textContent.includes("REQ-500"),"request id missing");
  await nodes.retryButton.listeners.click();expect(nodes.errorBox.textContent.includes("单据状态已变化"),"object message missing");expect(nodes.errorBox.textContent.includes("DOC_STALE"),"object code missing");
  await nodes.retryButton.listeners.click();expect(nodes.content.innerHTML==="NEW","success not rendered");expect(!nodes.printButton.disabled,"success did not unlock print");
  let resolveOld;const oldResponse=new Promise(resolve=>{{resolveOld=resolve}});let call=0;
  global.fetch=async()=>{{call+=1;if(call===1)return oldResponse;return{{ok:true,status:200,headers:{{get:()=>null}},text:async()=>JSON.stringify({{number:"LATEST"}})}}}};
  const oldLoad=page.load();const latestLoad=page.load();await latestLoad;
  resolveOld({{ok:true,status:200,headers:{{get:()=>null}},text:async()=>JSON.stringify({{number:"OLD"}})}});await oldLoad;
  expect(nodes.content.innerHTML==="LATEST","stale response replaced latest result");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "print-recovery-behavior.js")


def test_shared_print_recovery_rejects_invalid_id_without_request(tmp_path: Path) -> None:
    harness = f"""
const vm=require("vm");global.window=global;global.AbortController=class{{constructor(){{this.signal={{aborted:false}}}}abort(){{this.signal.aborted=true}}}};
const classList=()=>({{add(){{}},remove(){{}},toggle(){{}},contains(){{return false}}}});const node=()=>({{textContent:"",disabled:false,hidden:false,classList:classList(),listeners:{{}},addEventListener(t,f){{this.listeners[t]=f}}}});
const nodes=Object.fromEntries(["print","retry","load","error","content"].map(id=>[id,node()]));let fetchCount=0;global.fetch=async()=>{{fetchCount+=1}};
vm.runInThisContext({json.dumps(UTIL, ensure_ascii=False)});
const page=TmPrintRecovery.createPrintPage({{id:"abc",url:id=>`/${{id}}`,printButton:nodes.print,retryButton:nodes.retry,loadState:nodes.load,errorBox:nodes.error,content:nodes.content,loadingMessage:"读取",invalidMessage:"链接无效",authMessage:"登录失效",failureMessage:"失败",render(){{}},print(){{}}}});
page.load().then(()=>{{if(fetchCount!==0)throw new Error("invalid id fetched");if(!nodes.error.textContent.includes("链接无效"))throw new Error("invalid message missing");if(!nodes.retry.hidden)throw new Error("invalid id offered retry")}}).catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "print-recovery-invalid.js")


def test_quotation_and_contract_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    for name, source in (
        ("print-recovery.js", UTIL),
        ("quotation-print.js", _inline_script(QUOTATION)),
        ("contract-print.js", _inline_script(CONTRACT)),
    ):
        target = tmp_path / name
        target.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stderr

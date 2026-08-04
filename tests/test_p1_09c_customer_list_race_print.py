from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "static" / "customers.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", PAGE)
    assert match, name
    start = match.start()
    brace = PAGE.index(") {", match.end()) + 2
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(PAGE)):
        char = PAGE[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'", "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return PAGE[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


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


def test_customer_list_starts_with_safe_output_controls() -> None:
    for button_id in ("topExport", "topPrint", "exportBtn", "printBtn"):
        assert re.search(rf'<button[^>]*id="{button_id}"[^>]*disabled', PAGE)
    for marker in (
        "listController: null",
        "listGeneration: 0",
        "listReady: false",
        "new AbortController()",
        "generation !== state.listGeneration",
        "setCustomerListControls",
        "resetCustomerListView",
        "printCustomerList",
        "当前客户列表尚未读取完成",
    ):
        assert marker in PAGE
    assert 'addEventListener("click", () => window.print())' not in PAGE


def test_latest_customer_query_wins_and_old_finally_cannot_unlock(tmp_path: Path) -> None:
    functions = "\n".join(
        _function_source(name)
        for name in (
            "setCustomerListControls",
            "resetCustomerListView",
            "loadCustomers",
        )
    )
    harness = f"""
const state={{items:[],selected:null,page:1,pageSize:20,pages:1,total:0,keyword:"旧",status:"",loading:false,listReady:false,listController:null,listGeneration:0}};
class FakeAbortController{{constructor(){{this.signal={{aborted:false}}}}abort(){{this.signal.aborted=true}}}};global.AbortController=FakeAbortController;
const nodes=Object.fromEntries(["loadingState","customerRows","topExport","topPrint","exportBtn","printBtn","prevPage","nextPage"].map(id=>[id,{{id,hidden:false,textContent:"",innerHTML:"",disabled:false}}]));
const $=id=>nodes[id]||{{}};const renders=[];let toast="";
function renderRows(){{renders.push(state.items.map(row=>row.name).join(","));nodes.loadingState.hidden=true}}
function renderPagination(data){{state.page=data.page;state.pageSize=data.page_size;state.pages=data.pages;state.total=data.total}}
function renderSummary(data){{state.summary=data}}
function updateActions(){{}}
function showToast(message){{toast=message}}
let resolveOld,resolveNew;const oldPromise=new Promise(resolve=>resolveOld=resolve);const newPromise=new Promise(resolve=>resolveNew=resolve);let calls=0;
async function api(){{calls+=1;return calls===1?oldPromise:newPromise}}
{functions}
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const oldLoad=loadCustomers();state.keyword="新";const newLoad=loadCustomers();
  resolveOld({{items:[{{id:1,name:"旧结果"}}],pagination:{{page:1,page_size:20,pages:1,total:1}},summary:{{total:1}}}});await oldLoad;
  expect(state.loading,"old finally ended current loading");expect(!state.listReady,"old response marked list ready");
  resolveNew({{items:[{{id:2,name:"新结果"}}],pagination:{{page:1,page_size:20,pages:1,total:1}},summary:{{total:1}}}});await newLoad;
  expect(state.items.length===1&&state.items[0].name==="新结果","old response replaced latest result");expect(!state.loading,"latest request did not finish loading");expect(state.listReady,"latest success did not mark ready");expect(!nodes.printBtn.disabled,"valid latest result did not enable print");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "customer-list-latest-wins.js")


def test_customer_list_failure_clears_stale_counts_and_outputs(tmp_path: Path) -> None:
    functions = "\n".join(
        _function_source(name)
        for name in (
            "setCustomerListControls",
            "resetCustomerListView",
            "loadCustomers",
        )
    )
    harness = f"""
const state={{items:[{{id:8,name:"旧客户"}}],selected:{{id:8}},page:3,pageSize:20,pages:5,total:88,keyword:"失败",status:"",loading:false,listReady:true,listController:null,listGeneration:0}};
class FakeAbortController{{constructor(){{this.signal={{aborted:false}}}}abort(){{this.signal.aborted=true}}}};global.AbortController=FakeAbortController;
const nodes=Object.fromEntries(["loadingState","customerRows","topExport","topPrint","exportBtn","printBtn","prevPage","nextPage"].map(id=>[id,{{id,hidden:false,textContent:"",innerHTML:"",disabled:false}}]));
const $=id=>nodes[id]||{{}};let summary=null,pagination=null,toast="";
function renderRows(){{nodes.loadingState.hidden=true}}function renderPagination(data){{pagination=data;state.page=data.page;state.pageSize=data.page_size;state.pages=data.pages;state.total=data.total}}function renderSummary(data){{summary=data}}function updateActions(){{}}function showToast(message){{toast=message}}
async function api(){{throw new Error("服务器内部错误；HTTP 500；请求编号 REQ-CUSTOMER")}}
{functions}
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{await loadCustomers();expect(state.items.length===0,"stale rows kept after failure");expect(state.total===0&&state.pages===1&&state.page===1,"stale pagination kept after failure");expect(summary.total===0,"stale summary kept after failure");expect(!state.listReady,"failure marked list ready");expect(nodes.printBtn.disabled&&nodes.exportBtn.disabled,"failure enabled outputs");expect(nodes.loadingState.textContent.includes("REQ-CUSTOMER"),"request id not shown")}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "customer-list-failure-reset.js")


def test_customer_api_error_keeps_object_code_http_and_request_id(tmp_path: Path) -> None:
    functions = "\n".join(
        _function_source(name) for name in ("apiErrorDetail", "api")
    )
    harness = f"""
global.fetch=async()=>({{ok:false,status:409,headers:{{get:name=>name==="x-request-id"?"REQ-409":""}},text:async()=>JSON.stringify({{detail:{{message:"客户资料已变化",code:"CUSTOMER_STALE"}}}})}});
{functions}
api("/api/customer-management").then(()=>{{throw new Error("409 resolved")}}).catch(error=>{{if(!error.message.includes("客户资料已变化"))throw new Error("message missing");if(!error.message.includes("CUSTOMER_STALE"))throw new Error("code missing");if(!error.message.includes("HTTP 409"))throw new Error("status missing");if(!error.message.includes("REQ-409"))throw new Error("request id missing");if(error.status!==409)throw new Error("status property missing")}}).catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "customer-api-object-error.js")


def test_customer_page_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", PAGE, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "customers-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

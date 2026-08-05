from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "static" / "requisition-print.html").read_text(encoding="utf-8")


def _script() -> str:
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", PAGE, re.DOTALL)
    assert scripts
    return "\n".join(scripts)


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript behavior validation"
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


def test_requisition_print_has_loading_retry_and_safe_dom_contract() -> None:
    for marker in (
        'id="printButton"',
        'id="retryButton"',
        'id="loadState"',
        "正在读取报料单",
        "重新读取",
        "返回 ERP",
        "function parseErrorDetail",
        "async function requestPrintData",
        "async function loadRequisition",
        "Number.isInteger(batchId)",
        "无法连接 ERP 服务",
        'document.getElementById("number")',
        'document.getElementById("date")',
        'document.getElementById("supplier")',
        'document.getElementById("rows")',
    ):
        assert marker in PAGE
    assert 'id="printButton" disabled' in PAGE
    assert "document.body.innerHTML" not in PAGE
    assert "number.textContent" not in PAGE
    assert "date.textContent" not in PAGE
    assert "supplier.textContent" not in PAGE
    assert "rows.innerHTML" not in PAGE


def test_requisition_print_recovers_network_server_object_then_success(tmp_path: Path) -> None:
    script = _script()
    payload = {
        "sender": {"company_name": "测试纸箱厂", "address": "测试地址", "phone": "123"},
        "requisition_number": "REQ-001",
        "requisition_date": "2026-08-05",
        "supplier_name": "测试供应商",
        "items": [
            {
                "specification": "470×600",
                "crease_display": "100+200+100",
                "material": "K=A / B楞",
                "quantity": 50,
                "report_remark": "测试备注",
            }
        ],
    }
    harness = f"""
const vm=require("vm");
class FakeAbortController {{ constructor() {{ this.signal={{aborted:false}}; }} abort() {{ this.signal.aborted=true; }} }}
global.AbortController=FakeAbortController;
const classList=()=>{{const values=new Set(["hidden"]);return {{add:value=>values.add(value),remove:value=>values.delete(value),toggle:(value,force)=>force?values.add(value):values.delete(value),contains:value=>values.has(value)}}}};
function node(id){{return {{id,textContent:"",innerHTML:"",disabled:false,hidden:false,classList:classList(),listeners:{{}},addEventListener(type,fn){{this.listeners[type]=fn}}}}}}
const ids=["printButton","retryButton","loadState","sheet","company-name","number","date","supplier","rows","company-footer"];
const nodes=Object.fromEntries(ids.map(id=>[id,node(id)]));
global.document={{getElementById:id=>nodes[id],body:{{classList:classList()}}}};
global.location={{search:"?id=7",pathname:"/requisition-print.html",href:""}};
global.window={{print:()=>{{}}}};
global.TmTime={{formatBusinessDate:value=>String(value)}};
const responses=[
  {{kind:"network"}},
  {{ok:false,status:500,text:"broken"}},
  {{ok:false,status:409,text:JSON.stringify({{detail:{{message:"报料单状态已变化",code:"REQ_STALE"}}}})}},
  {{ok:true,status:200,text:JSON.stringify({json.dumps(payload, ensure_ascii=False)})}},
];
global.fetch=async()=>{{const next=responses.shift();if(next.kind==="network")throw new TypeError("Failed to fetch");return {{ok:next.ok,status:next.status,text:async()=>next.text}}}};
vm.runInThisContext({json.dumps(script, ensure_ascii=False)});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
(async()=>{{
  await tick();
  expect(nodes.loadState.textContent.includes("无法连接 ERP"),"network error must be Chinese");
  expect(nodes.printButton.disabled,"print must remain disabled after failure");
  await nodes.retryButton.listeners.click();
  expect(nodes.loadState.textContent.includes("HTTP 500"),"non-JSON error must retain HTTP status");
  await nodes.retryButton.listeners.click();
  expect(nodes.loadState.textContent.includes("报料单状态已变化"),"object detail message missing");
  expect(nodes.loadState.textContent.includes("REQ_STALE"),"object detail code missing");
  await nodes.retryButton.listeners.click();
  expect(!nodes.printButton.disabled,"print must be enabled after successful render");
  expect(nodes.number.textContent.includes("REQ-001"),"requisition number missing");
  expect(nodes.rows.innerHTML.includes("470×600"),"line specification missing");
  expect(nodes.loadState.classList.contains("hidden"),"load message must clear after success");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-requisition-print-recovery.js")


def test_requisition_print_invalid_id_never_calls_api(tmp_path: Path) -> None:
    script = _script()
    harness = f"""
const vm=require("vm");
const classList=()=>({{add:()=>{{}},remove:()=>{{}},toggle:()=>{{}},contains:()=>false}});
const nodes={{}};
for(const id of ["printButton","retryButton","loadState","sheet","company-name","number","date","supplier","rows","company-footer"])nodes[id]={{textContent:"",innerHTML:"",disabled:false,classList:classList(),listeners:{{}},addEventListener(type,fn){{this.listeners[type]=fn}}}};
global.document={{getElementById:id=>nodes[id],body:{{classList:classList()}}}};
global.location={{search:"?id=abc",pathname:"/requisition-print.html",href:""}};
global.window={{print:()=>{{}}}};
global.TmTime={{formatBusinessDate:value=>String(value)}};
let fetchCount=0;global.fetch=async()=>{{fetchCount+=1;throw new Error("must not fetch")}};
vm.runInThisContext({json.dumps(script, ensure_ascii=False)});
setImmediate(()=>{{if(fetchCount!==0){{console.error("invalid id issued a request");process.exit(1)}}if(!nodes.loadState.textContent.includes("链接无效")){{console.error("invalid id message missing");process.exit(1)}}}});
"""
    _run_node(harness, tmp_path, "p1-09c-requisition-print-invalid-id.js")


def test_requisition_print_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    target = tmp_path / "requisition-print.js"
    target.write_text(_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

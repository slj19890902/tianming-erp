from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "static" / "delivery-print.html").read_text(encoding="utf-8")


def _script() -> str:
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", PAGE, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    return scripts[0]


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


def test_delivery_print_has_loading_retry_and_disabled_print_contract() -> None:
    for marker in (
        'id="printButton" disabled',
        'id="retryButton"',
        'id="loadState"',
        "等待发货确认",
        "正在读取送货单",
        "重新读取",
        "function parseErrorDetail",
        "async function requestDeliveryPrint",
        "async function loadDelivery",
        "AbortController",
        "loadGeneration",
        "无法连接 ERP 服务",
    ):
        assert marker in PAGE


def test_delivery_print_recovers_network_server_object_then_success(tmp_path: Path) -> None:
    script = _script()
    payload = {
        "delivery_number": "TH-001",
        "delivery_date": "2026-08-05",
        "customer": {"name": "匿名客户"},
        "items": [],
    }
    harness = f"""
const vm=require("vm");
class FakeAbortController {{ constructor() {{ this.signal={{aborted:false}}; }} abort() {{ this.signal.aborted=true; }} }}
global.AbortController=FakeAbortController;
const classList=()=>{{const values=new Set(["hidden"]);return {{add:value=>values.add(value),remove:value=>values.delete(value),toggle:(value,force)=>force?values.add(value):values.delete(value),contains:value=>values.has(value)}}}};
function node(id){{return {{id,textContent:"",innerHTML:"",disabled:false,hidden:false,style:{{}},classList:classList(),listeners:{{}},addEventListener(type,fn){{this.listeners[type]=fn}}}}}}
const ids=["printButton","retryButton","loadState","errorBox","sheets","paperGuide"];
const nodes=Object.fromEntries(ids.map(id=>[id,node(id)]));
global.document={{
  getElementById:id=>nodes[id],
  querySelector:selector=>selector.includes("paperGuide")?nodes.paperGuide:null,
  documentElement:{{style:{{setProperty(){{}}}},dataset:{{}}}},
}};
global.window={{addEventListener(){{}},location:{{search:"?id=7&defer=1"}},print:()=>{{throw new Error("must not print during test")}}}};
global.CustomerDeliveryPrint={{controls(){{}}}};
global.TmTime={{formatBeijingDateTime:value=>String(value)}};
global.BroadcastChannel=undefined;
const responses=[
  {{kind:"network"}},
  {{ok:false,status:500,text:"broken",headers:{{get:()=>"REQ-500"}}}},
  {{ok:false,status:409,text:JSON.stringify({{detail:{{message:"送货单状态已变化",code:"DELIVERY_STALE"}}}}),headers:{{get:()=>"REQ-409"}}}},
  {{ok:true,status:200,text:JSON.stringify({json.dumps(payload, ensure_ascii=False)}),headers:{{get:()=>null}}}},
];
global.fetch=async()=>{{const next=responses.shift();if(next.kind==="network")throw new TypeError("Failed to fetch");return {{ok:next.ok,status:next.status,headers:next.headers,text:async()=>next.text}}}};
vm.runInThisContext({json.dumps(script, ensure_ascii=False)});
global.renderDelivery=data=>{{nodes.sheets.innerHTML=data.delivery_number;nodes.sheets.hidden=false}};
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
(async()=>{{
  await loadDelivery();
  expect(nodes.errorBox.textContent.includes("无法连接 ERP"),"network error must be Chinese");
  expect(nodes.printButton.disabled,"print must remain disabled after network error");
  await nodes.retryButton.listeners.click();
  expect(nodes.errorBox.textContent.includes("HTTP 500"),"non-JSON error must retain status");
  expect(nodes.errorBox.textContent.includes("REQ-500"),"request id must be retained");
  await nodes.retryButton.listeners.click();
  expect(nodes.errorBox.textContent.includes("送货单状态已变化"),"object detail message missing");
  expect(nodes.errorBox.textContent.includes("DELIVERY_STALE"),"object detail code missing");
  await nodes.retryButton.listeners.click();
  expect(!nodes.printButton.disabled,"print must enable only after successful render");
  expect(nodes.sheets.innerHTML==="TH-001","successful response not rendered");
  expect(nodes.loadState.hidden,"loading state must clear after success");
  expect(nodes.errorBox.hidden,"error state must clear after success");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-delivery-print-recovery.js")


def test_delivery_print_invalid_id_never_fetches_and_abort_cannot_retry(tmp_path: Path) -> None:
    script = _script()
    harness = f"""
const vm=require("vm");
const classList=()=>({{add(){{}},remove(){{}},toggle(){{}},contains(){{return false}}}});
function node(id){{return {{id,textContent:"",disabled:false,hidden:false,style:{{}},classList:classList(),listeners:{{}},addEventListener(type,fn){{this.listeners[type]=fn}}}}}}
const ids=["printButton","retryButton","loadState","errorBox","sheets","paperGuide"];
const nodes=Object.fromEntries(ids.map(id=>[id,node(id)]));
const channels=[];
global.BroadcastChannel=class{{constructor(name){{this.name=name;channels.push(this)}}postMessage(message){{this.last=message}}close(){{this.closed=true}}}};
global.AbortController=class{{constructor(){{this.signal={{aborted:false}}}}abort(){{this.signal.aborted=true}}}};
global.document={{getElementById:id=>nodes[id],querySelector:()=>nodes.paperGuide,documentElement:{{style:{{setProperty(){{}}}},dataset:{{}}}}}};
global.CustomerDeliveryPrint={{controls(){{}}}};
global.TmTime={{formatBeijingDateTime:value=>String(value)}};
let fetchCount=0;global.fetch=async()=>{{fetchCount+=1;throw new Error("must not fetch")}};
global.window={{addEventListener(){{}},location:{{search:"?id=abc&defer=1&open_token=uat"}},print(){{}}}};
vm.runInThisContext({json.dumps(script, ensure_ascii=False)});
(async()=>{{
  await loadDelivery();
  if(fetchCount!==0)throw new Error("invalid id issued a request");
  if(!nodes.errorBox.textContent.includes("链接无效"))throw new Error("invalid id message missing");
  if(!nodes.retryButton.hidden)throw new Error("invalid id must not offer retry");
  window.location.search="?id=7&defer=1&open_token=uat";
  channels[0].onmessage({{data:{{type:"abort",message:"发货事务失败"}}}});
  if(!nodes.printButton.disabled)throw new Error("abort must keep print disabled");
  if(!nodes.retryButton.hidden)throw new Error("dispatch abort must not offer data retry");
  if(!nodes.errorBox.textContent.includes("发货事务失败"))throw new Error("abort reason missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-delivery-print-invalid-abort.js")


def test_delivery_print_retry_discards_stale_response(tmp_path: Path) -> None:
    script = _script()
    harness = f"""
const vm=require("vm");
class FakeAbortController {{ constructor() {{ this.signal={{aborted:false}}; }} abort() {{ this.signal.aborted=true; }} }}
global.AbortController=FakeAbortController;
const classList=()=>({{add(){{}},remove(){{}},toggle(){{}},contains(){{return false}}}});
function node(id){{return {{id,textContent:"",innerHTML:"",disabled:false,hidden:false,style:{{}},classList:classList(),listeners:{{}},addEventListener(type,fn){{this.listeners[type]=fn}}}}}}
const ids=["printButton","retryButton","loadState","errorBox","sheets","paperGuide"];
const nodes=Object.fromEntries(ids.map(id=>[id,node(id)]));
global.document={{getElementById:id=>nodes[id],querySelector:()=>nodes.paperGuide,documentElement:{{style:{{setProperty(){{}}}},dataset:{{}}}}}};
global.window={{addEventListener(){{}},location:{{search:"?id=7&defer=1"}},print(){{}}}};
global.BroadcastChannel=undefined;
global.CustomerDeliveryPrint={{controls(){{}}}};
global.TmTime={{formatBeijingDateTime:value=>String(value)}};
let resolveFirst;
const first=new Promise(resolve=>{{resolveFirst=resolve}});
let call=0;
const response=value=>({{ok:true,status:200,headers:{{get:()=>null}},text:async()=>JSON.stringify({{delivery_number:value,customer:{{}},items:[]}})}});
global.fetch=async()=>{{call+=1;return call===1?first:response("NEW")}};
vm.runInThisContext({json.dumps(script, ensure_ascii=False)});
const rendered=[];global.renderDelivery=data=>{{rendered.push(data.delivery_number)}};
(async()=>{{
  const oldLoad=loadDelivery();
  const newLoad=loadDelivery();
  await newLoad;
  resolveFirst(response("OLD"));
  await oldLoad;
  if(rendered.join(",")!=="NEW")throw new Error(`stale response rendered: ${{rendered.join(",")}}`);
  if(nodes.printButton.disabled)throw new Error("latest successful response must enable print");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-delivery-print-stale.js")


def test_delivery_print_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    target = tmp_path / "delivery-print.js"
    target.write_text(_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

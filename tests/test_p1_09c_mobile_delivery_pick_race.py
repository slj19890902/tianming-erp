from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_delivery_pick.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*?)</script>", MOBILE, re.DOTALL)
    assert match is not None
    return match.group(1)


def _between(start: str, end: str) -> str:
    source = _script()
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


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


def test_mobile_delivery_pick_has_race_retry_and_busy_guards() -> None:
    for marker in (
        "let listGeneration=0",
        "detailGeneration=0",
        "new AbortController()",
        "generation!==listGeneration",
        "generation!==detailGeneration",
        "const busyItems=new Set()",
        "let taskActionBusy=false",
        "if(busyItems.has(id)||taskActionBusy) return",
        "if(taskActionBusy||busyItems.size) return",
        'id="submitTaskButton"',
        'id="refreshTaskButton"',
        "showTaskFailure",
        "重试",
    ):
        assert marker in MOBILE


def test_fast_task_switch_keeps_only_latest_detail(tmp_path: Path) -> None:
    select_source = _between("async function selectTask(id)", "async function showTaskList()")
    harness = f"""
class FakeAbortController {{ constructor() {{ this.signal={{aborted:false}}; }} abort() {{ this.signal.aborted=true; }} }}
global.AbortController=FakeAbortController;
let taskId=null,task=null,detailGeneration=0,detailController=null,expectedPrintVersion=null;
const nodes={{taskChooser:{{hidden:false}},taskDetail:{{hidden:true}},taskMeta:{{textContent:""}},locationGroups:{{innerHTML:""}},groups:{{innerHTML:""}},error:{{textContent:""}}}};
global.document={{getElementById:id=>nodes[id]}};
global.location={{href:"http://erp/mobile/delivery-pick.html"}};
global.history={{replaceState:()=>{{}}}};
const calls=[];
function request(url,options){{return new Promise((resolve,reject)=>calls.push({{url,options,resolve,reject}}))}}
function normalize(body){{return body.task||body}}
let rendered=0;
function render(){{rendered+=1}}
function showTaskLoading(){{nodes.taskMeta.textContent="正在读取"}}
function showTaskFailure(){{nodes.error.textContent="failed"}}
function handleAuthError(){{return false}}
{select_source}
(async()=>{{
  const first=selectTask(1);
  const second=selectTask(2);
  calls[0].resolve({{id:1,items:[]}});
  await first;
  if(task!==null)throw new Error("older task response replaced current selection");
  calls[1].resolve({{id:2,items:[]}});
  await second;
  if(task?.id!==2)throw new Error("latest task detail was not retained");
  if(rendered!==1)throw new Error("only the latest detail should render");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-delivery-detail-race.js")


def test_item_update_ignores_duplicate_taps(tmp_path: Path) -> None:
    update_source = _between("async function updateItem(itemId,status)", "async function completePlanned()")
    harness = f"""
const busyItems=new Set(); let taskActionBusy=false;
const input={{value:"5",disabled:false}};
const buttons=[{{disabled:false}},{{disabled:false}},{{disabled:false}}];
const card={{querySelector:selector=>selector===".picked-qty"?input:null,querySelectorAll:()=>buttons}};
global.document={{querySelector:()=>card,getElementById:()=>({{textContent:""}})}};
let task={{id:8,items:[{{id:3,planned_quantity:5,pick_status:"pending"}}]}};
let requestCount=0,resolveRequest;
function request(){{requestCount+=1;return new Promise(resolve=>{{resolveRequest=resolve}})}}
function render(){{}}
function toast(){{}}
{update_source}
(async()=>{{
  const first=updateItem(3,"picked");
  const second=updateItem(3,"picked");
  if(requestCount!==1)throw new Error("duplicate tap sent more than one request");
  resolveRequest({{item:{{id:3,pick_status:"picked",picked_quantity:5}}}});
  await Promise.all([first,second]);
  if(busyItems.size)throw new Error("busy item was not released");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-delivery-item-busy.js")


def test_error_parser_handles_object_non_json_and_network(tmp_path: Path) -> None:
    formatter = _between("function formatErrorDetail(detail)", "async function request(url")
    request_source = _between("async function request(url", "function normalize(body)")
    harness = f"""
{formatter}
{request_source}
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  global.fetch=async()=>({{ok:false,status:409,text:async()=>JSON.stringify({{detail:{{message:"拿货任务已变更",code:"PICK_STALE"}}}})}});
  try{{await request("/object")}}catch(error){{expect(error.message.includes("拿货任务已变更"),"object message missing");expect(error.message.includes("PICK_STALE"),"object code missing")}}
  global.fetch=async()=>({{ok:false,status:500,text:async()=>"broken"}});
  try{{await request("/server")}}catch(error){{expect(error.message.includes("HTTP 500"),"HTTP status missing")}}
  global.fetch=async()=>{{throw new TypeError("Failed to fetch")}};
  try{{await request("/offline")}}catch(error){{expect(error.message.includes("无法连接 ERP"),"network message missing");return}}
  throw new Error("network request must fail");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-delivery-errors.js")


def test_mobile_delivery_pick_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    target = tmp_path / "mobile_delivery_pick.js"
    target.write_text(_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

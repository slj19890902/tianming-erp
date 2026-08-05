from __future__ import annotations

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
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_customer_detail_has_latest_request_and_close_guards() -> None:
    method = _function_source("openDetail")
    close = _function_source("closeDetail")

    for marker in (
        "detailController",
        "detailGeneration",
        "new AbortController()",
        "generation !== state.detailGeneration",
        "controller.signal.aborted",
    ):
        assert marker in PAGE
    assert "state.detailController.abort()" in close
    assert "state.detailGeneration += 1" in close
    assert "重新读取客户详情" in method


def test_customer_detail_runtime_keeps_latest_and_close_invalidates(tmp_path: Path) -> None:
    functions = "\n".join((_function_source("openDetail"), _function_source("closeDetail")))
    harness = f"""
const state={{detailCustomer:null,detailController:null,detailGeneration:0}};
class FakeAbortController{{constructor(){{this.signal={{aborted:false}}}}abort(){{this.signal.aborted=true}}}};global.AbortController=FakeAbortController;
const nodes={{drawerMask:{{hidden:true}},drawerNewOrder:{{hidden:true}},drawerTitle:{{textContent:""}},drawerBody:{{innerHTML:""}}}};
const $=id=>nodes[id];const escapeHtml=value=>String(value??"");const statusText=value=>value;const cleanPhone=value=>value;const taxText=value=>value;const detailValue=()=>"";
let calls=[];const pending=[];async function api(url,options){{return new Promise(resolve=>pending.push({{url,options,resolve}}))}}
{functions}
const customer=(id,name)=>({{customer:{{id,name,status:"active",product_count:0,order_count:0,history_order_count:0,file_count:0}},products:[]}});
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=openDetail(1);const second=openDetail(2);
  pending[1].resolve(customer(2,"客户B"));await second;
  pending[0].resolve(customer(1,"客户A"));await first;
  expect(state.detailCustomer?.id===2,"旧详情覆盖了最后选择");expect(nodes.drawerTitle.textContent==="客户B","标题被旧详情覆盖");
  const third=openDetail(3);closeDetail();pending[2].resolve(customer(3,"客户C"));await third;
  expect(nodes.drawerMask.hidden,"关闭后旧响应重新打开抽屉");expect(state.detailCustomer===null,"关闭后旧响应写回详情");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "customer-detail-race.js")


def test_customer_save_and_status_are_single_flight_and_preserve_success() -> None:
    save = _function_source("saveCustomer")
    status = _function_source("changeStatus")

    assert "customerSavePending" in save
    assert "客户资料正在保存，请勿重复点击" in save
    assert "保存中…" in save
    assert "statusPending" in status
    assert "客户状态正在处理，请勿重复点击" in status
    assert "const targetCustomer" in status
    for method in (save, status):
        assert "业务已成功，请勿重复提交" in method
        assert "手动刷新核对" in method


def test_customer_list_loading_disables_and_latest_finally_restores_master_actions() -> None:
    method = _function_source("loadCustomers")
    loading_start = method.index("state.loading = true")
    first_action_update = method.index("updateActions()", loading_start)
    loading_end = method.index("state.loading = false")
    final_action_update = method.index("updateActions()", loading_end)

    assert loading_start < first_action_update < loading_end < final_action_update


def test_customer_save_runtime_blocks_duplicate_and_reports_refresh_failure(tmp_path: Path) -> None:
    save = _function_source("saveCustomer")
    harness = f"""
const state={{customerSavePending:false,selected:null,items:[]}};
const nodes={{customerId:{{value:""}},customerName:{{focus(){{}}}},saveCustomer:{{disabled:false,textContent:"保存客户资料"}}}};const $=id=>nodes[id]||{{}};
const formPayload=()=>({{name:"新客户"}});let calls=0,release;async function api(){{calls+=1;return new Promise(resolve=>release=resolve)}}
async function loadCustomers(){{return false}}function closeForm(){{}}function updateActions(){{}}const messages=[];function showToast(message,error=false){{messages.push({{message,error}})}}
{save}
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{const first=saveCustomer();const duplicate=saveCustomer();expect(calls===1,"重复保存发出了第二个请求");release({{customer:{{id:9,name:"新客户"}}}});await Promise.all([first,duplicate]);expect(messages.some(row=>row.message.includes("业务已成功，请勿重复提交")),"刷新失败被误报为保存失败");expect(!state.customerSavePending,"保存结束未释放门禁")}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "customer-save-single-flight.js")


def test_customer_status_runtime_freezes_target_and_blocks_duplicate(tmp_path: Path) -> None:
    status = _function_source("changeStatus")
    harness = f"""
const state={{selected:{{id:1,name:"客户A",status:"active"}},statusPending:false}};const nodes={{statusBtn:{{disabled:false,textContent:"停用"}}}};const $=id=>nodes[id];global.window={{confirm:()=>true}};
let urls=[],release;async function api(url){{urls.push(url);return new Promise(resolve=>release=resolve)}}async function loadCustomers(){{return false}}function updateActions(){{}}const messages=[];function showToast(message,error=false){{messages.push({{message,error}})}}
{status}
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{const first=changeStatus();state.selected={{id:2,name:"客户B",status:"active"}};const duplicate=changeStatus();expect(urls.length===1&&urls[0].includes("/1/status"),"状态操作未冻结原客户或发生重复请求");release({{}});await Promise.all([first,duplicate]);expect(messages.some(row=>row.message.includes("业务已成功，请勿重复提交")),"状态写入成功后的刷新失败提示缺失");expect(!state.statusPending,"状态操作结束未释放门禁")}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "customer-status-single-flight.js")

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.start() : match.end() + next_method.start()]


def _method_contents(name: str) -> str:
    method = _method_body(name)
    return method.split("{", 1)[1].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for requisition void action validation"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_void_ui_has_shared_state_lock_and_authoritative_reconcile() -> None:
    assert 'requisitionVoidActionState:{action:"",sourceType:"",targetId:null,targetKey:"",documentNumber:"",committed:false,outcomeUncertain:false,result:null}' in INDEX
    assert "报料已经作废或撤销，但页面刷新失败" in INDEX
    assert "上次报料作废结果暂不确定" in INDEX
    assert "核对报料状态" in INDEX
    assert INDEX.count("requisitionVoidActionBlocked()") >= 6
    assert "requisitionVoidActionLabel" in INDEX


def test_shared_void_executor_is_single_flight_and_classifies_results(tmp_path: Path) -> None:
    body = _method_contents("executeRequisitionVoidAction")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
function context(){{const messages=[];return {{messages,requisitionVoidActionState:{{action:'',sourceType:'',targetId:null,targetKey:'',documentNumber:'',committed:false,outcomeUncertain:false,result:null}},requisitionVoidActionBlocked(){{const s=this.requisitionVoidActionState;return Boolean(s.action||s.committed||s.outcomeUncertain);}},requisitionVoidActionLabel(){{return '正在作废…';}},resetRequisitionVoidActionState(){{this.requisitionVoidActionState={{action:'',sourceType:'',targetId:null,targetKey:'',documentNumber:'',committed:false,outcomeUncertain:false,result:null}};}},showToast(message,danger=false){{messages.push({{message,danger}});}},errorMessage(error){{return error?.message||String(error);}}}};}}
const run=new AsyncFunction('options',{json.dumps(body, ensure_ascii=False)});
(async()=>{{let finish;let writes=0;const first=context();const pending=run.call(first,{{action:'void',sourceType:'supplier_order',targetId:17,targetKey:'supplier:17',documentNumber:'SO-17',submit:()=>{{writes++;return new Promise(resolve=>finish=resolve);}},refresh:async()=>true,successMessage:'ok'}});await Promise.resolve();const duplicate=await run.call(first,{{action:'void',sourceType:'composite',targetId:99,targetKey:'batch:99',documentNumber:'B-99',submit:async()=>({{}}),refresh:async()=>true}});if(duplicate!==false||writes!==1||first.requisitionVoidActionState.targetId!==17||first.requisitionVoidActionState.documentNumber!=='SO-17') throw new Error('void was duplicated or target changed');finish({{data:{{status:'voided'}}}});if(await pending!==true||first.requisitionVoidActionBlocked()) throw new Error('success did not unlock');
const committed=context();if(await run.call(committed,{{action:'void',sourceType:'supplier_order',targetId:17,targetKey:'supplier:17',documentNumber:'SO-17',submit:async()=>({{data:{{}}}}),refresh:async()=>false}})!==true||!committed.requisitionVoidActionState.committed||!committed.messages.some(row=>row.message.includes('已经作废或撤销'))) throw new Error('committed result was lost');
const unknown=context();if(await run.call(unknown,{{action:'void',sourceType:'supplier_order',targetId:17,targetKey:'supplier:17',documentNumber:'SO-17',submit:async()=>{{throw new Error('network');}},refresh:async()=>true}})!==false||!unknown.requisitionVoidActionState.outcomeUncertain||!unknown.messages.some(row=>row.message.includes('结果暂不确定'))) throw new Error('unknown result was lost');
for(const status of [400,403,404,409,422]){{const explicit=context();const error=new Error('explicit');error.response={{status}};if(await run.call(explicit,{{action:'void',sourceType:'supplier_order',targetId:17,targetKey:'supplier:17',documentNumber:'SO-17',submit:async()=>{{throw error;}},refresh:async()=>true,errorPrefix:'作废失败：'}})!==false||explicit.requisitionVoidActionBlocked()) throw new Error(`explicit ${{status}} did not unlock`);}}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-void-executor.js")


def test_all_void_entry_points_use_shared_executor_and_freeze_targets() -> None:
    for name in (
        "cancelRequisition",
        "voidReportedCompositeRequisition",
        "voidReportedCompositeLine",
        "voidSupplierOrder",
    ):
        body = _method_body(name)
        assert "executeRequisitionVoidAction" in body, name
        assert "const targetId = Number(" in body, name
    assert "/api/requisition/items/${targetId}/cancel" in _method_body("cancelRequisition")
    assert "/api/requisition/batches/${targetId}/void" in _method_body("voidReportedCompositeRequisition")
    assert "/api/requisition/batch-items/${targetId}/void" in _method_body("voidReportedCompositeLine")
    assert "/api/requisition/supplier-orders/${targetId}/void" in _method_body("voidSupplierOrder")


def test_void_keeps_one_confirmation_and_no_reason_input() -> None:
    for name in (
        "cancelRequisition",
        "voidReportedCompositeRequisition",
        "voidReportedCompositeLine",
        "voidSupplierOrder",
    ):
        body = _method_body(name)
        assert body.count("confirmOriginalBusinessAction(") == 1, name
        assert "confirm(" not in body, name
        assert "prompt(" not in body, name


def test_reconcile_unlocks_only_after_all_authoritative_lists_refresh(tmp_path: Path) -> None:
    body = _method_contents("refreshRequisitionVoidState")
    assert "results.some(result => result === false)" in body
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
function context(ok){{const messages=[];let calls=0;const load=async()=>{{calls++;if(!ok&&calls===2) throw new Error('refresh failed');}};return {{messages,get calls(){{return calls;}},requisitionVoidActionState:{{action:'',sourceType:'supplier_order',targetId:17,targetKey:'supplier:17',documentNumber:'SO-17',committed:true,outcomeUncertain:false,result:{{}}}},loadRequisition:load,loadReportedDocuments:load,loadSupplierOrders:load,resetRequisitionVoidActionState(){{this.requisitionVoidActionState={{action:'',sourceType:'',targetId:null,targetKey:'',documentNumber:'',committed:false,outcomeUncertain:false,result:null}};}},showToast(message,danger=false){{messages.push({{message,danger}});}},errorMessage(error){{return error?.message||String(error);}}}};}}
const fn=new AsyncFunction({json.dumps(body, ensure_ascii=False)});
(async()=>{{const failed=context(false);if(await fn.call(failed)!==false||!failed.requisitionVoidActionState.committed||failed.requisitionVoidActionState.action) throw new Error('failed reconcile unlocked');const passed=context(true);if(await fn.call(passed)!==true||passed.requisitionVoidActionState.committed||passed.calls!==3||!passed.messages.some(row=>row.message.includes('已核对'))) throw new Error('successful reconcile did not refresh all lists');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-void-reconcile.js")

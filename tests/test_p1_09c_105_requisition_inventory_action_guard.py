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
    assert node, "Node.js is required for requisition inventory action validation"
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


def test_requisition_inventory_ui_exposes_one_shared_lock_and_reconcile() -> None:
    assert 'requisitionInventoryActionState:{action:"",committed:false,outcomeUncertain:false,context:"",orderItemId:null,bomSnapshotId:null,targetKey:"",result:null}' in INDEX
    assert "库存已经使用，但页面刷新失败" in INDEX
    assert "上次库存操作结果暂不确定" in INDEX
    assert "核对待报料库存状态" in INDEX
    assert INDEX.count("requisitionInventoryActionBlocked()") >= 10
    close_modal = _method_body("closeModal")
    assert "requisitionInventoryActionBlocked()" in close_modal
    assert "请先核对待报料库存状态" in close_modal


def test_shared_executor_is_single_flight_and_classifies_results(tmp_path: Path) -> None:
    body = _method_contents("executeRequisitionInventoryAction")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
function context(){{const messages=[];return {{messages,requisitionInventoryActionState:{{action:'',committed:false,outcomeUncertain:false,context:'',orderItemId:null,bomSnapshotId:null,targetKey:'',result:null}},requisitionInventoryActionBlocked(){{const state=this.requisitionInventoryActionState;return Boolean(state.action||state.committed||state.outcomeUncertain);}},requisitionInventoryActionLabel(){{return '正在处理库存…';}},resetRequisitionInventoryActionState(){{this.requisitionInventoryActionState={{action:'',committed:false,outcomeUncertain:false,context:'',orderItemId:null,bomSnapshotId:null,targetKey:'',result:null}};}},showToast(message,danger=false){{messages.push({{message,danger}});}},errorMessage(error){{return error?.message||String(error);}}}};}}
const run=new AsyncFunction('options',{json.dumps(body, ensure_ascii=False)});
(async()=>{{
  let finish;let submits=0;const first=context();const pending=run.call(first,{{action:'finished',context:'pending',orderItemId:17,bomSnapshotId:3,targetKey:'17:3',submit:()=>{{submits++;return new Promise(resolve=>finish=resolve);}},refresh:async()=>true,successMessage:'ok',errorPrefix:'失败：'}});await Promise.resolve();const duplicate=await run.call(first,{{action:'board',context:'pending',orderItemId:99,submit:async()=>({{}}),refresh:async()=>true}});if(duplicate!==false||submits!==1||first.requisitionInventoryActionState.orderItemId!==17||first.requisitionInventoryActionState.bomSnapshotId!==3) throw new Error('shared action was not single-flight or frozen');finish({{data:{{id:1}}}});if(await pending!==true||first.requisitionInventoryActionBlocked()) throw new Error('successful action did not unlock');
  const committed=context();if(await run.call(committed,{{action:'finished',context:'pending',orderItemId:17,submit:async()=>({{data:{{id:2}}}}),refresh:async()=>false,successMessage:'ok'}})!==true||!committed.requisitionInventoryActionState.committed||!committed.messages.some(row=>row.message.includes('库存已经使用'))) throw new Error('committed refresh failure was not retained');
  const unknown=context();if(await run.call(unknown,{{action:'board',context:'pending',orderItemId:18,submit:async()=>{{throw new Error('network down');}},refresh:async()=>true}})!==false||!unknown.requisitionInventoryActionState.outcomeUncertain||!unknown.messages.some(row=>row.message.includes('结果暂不确定'))) throw new Error('network unknown was not retained');
  for(const status of [400,409,422]){{const explicit=context();const error=new Error('explicit');error.response={{status}};if(await run.call(explicit,{{action:'semi',context:'supplier_draft',orderItemId:19,submit:async()=>{{throw error;}},refresh:async()=>true,errorPrefix:'失败：'}})!==false||explicit.requisitionInventoryActionBlocked()) throw new Error(`explicit ${{status}} did not unlock`);}}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-inventory-executor.js")


def test_all_six_inventory_entry_points_use_the_shared_executor() -> None:
    methods = (
        "autoCoverBomComponent",
        "autoUseLateFinishedInventory",
        "autoUseCustomerBoardPreparation",
        "autoCoverCompositeDraftLine",
        "confirmDraftLateFinishedInventory",
        "confirmDraftSemiInventory",
    )
    for name in methods:
        body = _method_body(name)
        assert "executeRequisitionInventoryAction" in body, name
        assert "createIdempotencyKey" in body, name
    assert "/auto-use-finished-inventory" in _method_body("autoUseLateFinishedInventory")
    assert "/auto-use-customer-board-preparation" in _method_body("autoUseCustomerBoardPreparation")
    assert "/reserve-from-pending" in _method_body("confirmDraftSemiInventory")


def test_semi_inventory_payload_is_frozen_before_request(tmp_path: Path) -> None:
    body = _method_contents("confirmDraftSemiInventory")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;globalThis.confirm=()=>true;let keyCalls=0;globalThis.createIdempotencyKey=()=>`semi-${{++keyCalls}}`;const calls=[];globalThis.axios={{post:async(url,payload)=>{{calls.push({{url,payload}});return {{data:{{allocated_requirement_quantity:6}}}};}}}};
const candidate={{lot_id:31,lot_number:'S-31',version:4,deductible_requirement_quantity:6,warning_codes:['CHECK'],signature_differences:[]}};const option={{order_item_id:19,component_type:'lining',remaining_requirement_quantity:8,_override:false}};
const vm={{modal:{{type:'supplierRequisitionDraft'}},requisitionInventoryActionState:{{action:'',committed:false,outcomeUncertain:false}},requisitionInventoryActionBlocked(){{return false;}},draftSemiInventoryCandidates:()=>[candidate],draftSemiInventoryNeedsOverride:()=>false,draftSemiInventoryNeedsReverseCreaseAdmin:()=>false,supplierDraftSemiInventoryOptions:()=>[option],showToast(){{}},errorMessage(error){{return error?.message||String(error);}},executeRequisitionInventoryAction:async function(config){{candidate.lot_id=99;candidate.version=10;option.order_item_id=77;return await config.submit();}}}};
const fn=new AsyncFunction('option','selectedCandidate',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{await fn(option,candidate);if(calls.length!==1||calls[0].url!=='/api/requisition/semi-inventory/reserve-from-pending') throw new Error('missing semi request');const payload=calls[0].payload;if(payload.order_item_id!==19||payload.requested_requirement_quantity!==6||payload.lots[0].lot_id!==31||payload.lots[0].expected_version!==4||payload.idempotency_key!=='semi-1') throw new Error('semi payload changed after confirmation');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-semi-payload.js")


def test_composite_late_result_cannot_mutate_replaced_draft_line(tmp_path: Path) -> None:
    body = _method_contents("autoCoverCompositeDraftLine")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;globalThis.createIdempotencyKey=()=> 'bom-key';globalThis.axios={{post:async()=>({{data:{{finished_reserved_piece_qty:8,semi_finished_reserved_piece_qty:2,remaining_required_piece_qty:0}}}})}};
const oldLine={{order_item_id:19,bom_snapshot_id:31,finished_inventory_reserved_qty:0,semi_finished_reserved_piece_qty:0,remaining_required_piece_qty:10}};const newLine={{order_item_id:29,bom_snapshot_id:41,finished_inventory_reserved_qty:4}};
const vm={{modal:{{type:'requisition'}},requisitionForm:{{items:[oldLine]}},showToast(){{}},errorMessage(error){{return error?.message||String(error);}},recalculateCompositeDraftLine(){{throw new Error('stale line was recalculated');}},executeRequisitionInventoryAction:async function(config){{const response=await config.submit();this.requisitionForm={{items:[newLine]}};const refreshed=await config.refresh(response);if(refreshed!==false) throw new Error('stale target was accepted');return true;}}}};
const fn=new AsyncFunction('line',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{await fn(oldLine);if(oldLine.finished_inventory_reserved_qty!==0||newLine.finished_inventory_reserved_qty!==4) throw new Error('late response overwrote a draft line');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-composite-stale.js")


def test_reconcile_unlocks_only_after_authoritative_refresh(tmp_path: Path) -> None:
    body = _method_contents("refreshRequisitionInventoryState")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
function context(ok){{const messages=[];return {{messages,modal:null,requisitionInventoryActionState:{{action:'',committed:true,outcomeUncertain:false,context:'pending',orderItemId:19,bomSnapshotId:null,targetKey:'19',result:{{}}}},async loadRequisition(){{if(!ok) throw new Error('refresh failed');}},resetRequisitionInventoryActionState(){{this.requisitionInventoryActionState={{action:'',committed:false,outcomeUncertain:false,context:'',orderItemId:null,bomSnapshotId:null,targetKey:'',result:null}};}},showToast(message,danger=false){{messages.push({{message,danger}});}},errorMessage(error){{return error?.message||String(error);}}}};}}
const fn=new AsyncFunction({json.dumps(body, ensure_ascii=False)});
(async()=>{{const failed=context(false);if(await fn.call(failed)!==false||!failed.requisitionInventoryActionState.committed||failed.requisitionInventoryActionState.action) throw new Error('failed reconcile unlocked');const passed=context(true);if(await fn.call(passed)!==true||passed.requisitionInventoryActionState.committed||!passed.messages.some(row=>row.message.includes('已核对'))) throw new Error('successful reconcile did not unlock');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-inventory-reconcile.js")

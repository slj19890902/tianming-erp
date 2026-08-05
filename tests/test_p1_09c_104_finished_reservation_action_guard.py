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
    assert node, "Node.js is required for reservation action validation"
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


def test_finished_reservation_ui_locks_conflicting_actions_and_exposes_reconcile() -> None:
    assert "finishedReservationActionState:{action:\"\",committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}" in INDEX
    assert "核对库存状态" in INDEX
    assert "库存操作已经完成，但页面刷新失败" in INDEX
    assert "上次库存操作结果暂不确定" in INDEX
    assert "finishedReservationActionBlocked()" in INDEX
    assert "finishedReservationActionLabel" in INDEX
    close_modal = _method_body("closeModal")
    assert "finishedReservationActionBlocked()" in close_modal
    assert "请先核对库存状态" in close_modal


def test_reserve_is_single_flight_and_freezes_inventory_payload(tmp_path: Path) -> None:
    body = _method_contents("confirmFinishedInventoryReservation")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const finishes=[];const calls=[];let keyCalls=0;globalThis.createIdempotencyKey=()=>`reserve-key-${{++keyCalls}}`;globalThis.confirm=()=>true;
globalThis.axios={{post:(url,payload)=>{{calls.push({{url,payload}});return new Promise(resolve=>finishes.push(resolve));}}}};
const messages=[];const vm={{
  finishedReservationActionState:{{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}},
  orderItemForm:{{id:19}},orderItemOrder:{{id:7}},
  finishedReservationActionBlocked(){{const state=this.finishedReservationActionState;return Boolean(state.action||state.committed||state.outcomeUncertain);}},
  resetFinishedReservationActionState(){{this.finishedReservationActionState={{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}};}},
  async reloadFinishedInventoryState(){{return true;}},showToast(message,danger=false){{messages.push({{message,danger}});}},errorMessage(error){{return error?.message||String(error);}},
}};
const reserve=new AsyncFunction('candidate',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{
  const candidate={{lot_id:31,lot_number:'LOT-31',_reserve_qty:8,version:4,warning_codes:['GENERAL_FINISHED_STOCK']}};
  const first=reserve(candidate);const duplicatePromise=reserve({{lot_id:99,lot_number:'LOT-99',_reserve_qty:1,version:1,warning_codes:[]}});await Promise.resolve();
  candidate.lot_id=66;candidate._reserve_qty=99;candidate.version=9;
  if(calls.length!==1||calls[0].url!=='/api/warehouse/finished/reservations') throw new Error('reserve was duplicated or target changed');
  const payload=calls[0].payload;if(payload.order_item_id!==19||payload.inventory_lot_id!==31||payload.quantity!==8||payload.expected_version!==4||payload.idempotency_key!=='reserve-key-1'||payload.warning_acknowledged_codes[0]!=='GENERAL_FINISHED_STOCK') throw new Error('reserve payload was not frozen');
  finishes[0]({{data:{{id:51,status:'active'}}}});const result=await first;const duplicate=await duplicatePromise;
  if(result!==true||duplicate!==false||vm.finishedReservationActionBlocked()) throw new Error('reserve did not finish cleanly');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "finished-reserve-single-flight.js")


def test_release_is_single_flight_and_freezes_reservation(tmp_path: Path) -> None:
    body = _method_contents("releaseFinishedInventoryReservation")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const finishes=[];const calls=[];let keyCalls=0;globalThis.createIdempotencyKey=()=>`release-key-${{++keyCalls}}`;globalThis.confirm=()=>true;
globalThis.axios={{post:(url,payload)=>{{calls.push({{url,payload}});return new Promise(resolve=>finishes.push(resolve));}}}};
const vm={{finishedReservationActionState:{{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}},orderItemForm:{{id:19}},orderItemOrder:{{id:7}},finishedReservationActionBlocked(){{const state=this.finishedReservationActionState;return Boolean(state.action||state.committed||state.outcomeUncertain);}},resetFinishedReservationActionState(){{this.finishedReservationActionState={{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}};}},async reloadFinishedInventoryState(){{return true;}},showToast(){{}},errorMessage(error){{return error?.message||String(error);}}}};
const release=new AsyncFunction('reservation',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{const row={{id:71,lot_number:'LOT-71',reserved_stock_quantity:6}};const first=release(row);const duplicatePromise=release({{id:72,lot_number:'LOT-72',reserved_stock_quantity:1}});await Promise.resolve();row.id=99;row.reserved_stock_quantity=88;if(calls.length!==1||calls[0].url!=='/api/warehouse/reservations/71/release'||calls[0].payload.idempotency_key!=='release-key-1') throw new Error('release target or key was not frozen');finishes[0]({{data:{{id:71,status:'released'}}}});if(await first!==true||await duplicatePromise!==false||vm.finishedReservationActionBlocked()) throw new Error('release did not finish cleanly');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "finished-release-single-flight.js")


def test_committed_refresh_failure_and_network_unknown_stay_blocked(tmp_path: Path) -> None:
    reserve_body = _method_contents("confirmFinishedInventoryReservation")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;globalThis.createIdempotencyKey=()=> 'stable-key';globalThis.confirm=()=>true;
function context(){{const messages=[];return {{messages,finishedReservationActionState:{{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}},orderItemForm:{{id:19}},orderItemOrder:{{id:7}},finishedReservationActionBlocked(){{const state=this.finishedReservationActionState;return Boolean(state.action||state.committed||state.outcomeUncertain);}},resetFinishedReservationActionState(){{this.finishedReservationActionState={{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}};}},showToast(message,danger=false){{messages.push({{message,danger}});}},errorMessage(error){{return error?.message||String(error);}}}};}}
const candidate={{lot_id:31,lot_number:'LOT-31',_reserve_qty:8,version:4,warning_codes:[]}};
(async()=>{{
  globalThis.axios={{post:async()=>({{data:{{id:51,status:'active'}}}})}};const committed=context();committed.reloadFinishedInventoryState=async()=>false;const committedSave=new AsyncFunction('candidate',{json.dumps(reserve_body, ensure_ascii=False)}).bind(committed);if(await committedSave(candidate)!==true||!committed.finishedReservationActionState.committed||!committed.messages.some(row=>row.message.includes('已经完成')&&row.message.includes('核对库存状态'))) throw new Error('committed refresh failure was not preserved');
  globalThis.axios={{post:async()=>{{throw new Error('network down');}}}};const unknown=context();unknown.reloadFinishedInventoryState=async()=>true;const unknownSave=new AsyncFunction('candidate',{json.dumps(reserve_body, ensure_ascii=False)}).bind(unknown);if(await unknownSave(candidate)!==false||!unknown.finishedReservationActionState.outcomeUncertain||!unknown.messages.some(row=>row.message.includes('结果暂不确定'))) throw new Error('network unknown was not preserved');if(await unknownSave(candidate)!==false) throw new Error('unknown result was allowed to retry');
  for(const status of [400,409,422]){{globalThis.axios={{post:async()=>{{const error=new Error('explicit failure');error.response={{status}};throw error;}}}};const explicit=context();explicit.reloadFinishedInventoryState=async()=>true;const explicitSave=new AsyncFunction('candidate',{json.dumps(reserve_body, ensure_ascii=False)}).bind(explicit);if(await explicitSave(candidate)!==false||explicit.finishedReservationActionBlocked()) throw new Error(`explicit ${{status}} failure did not unlock retry`);}}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "finished-reserve-results.js")


def test_release_refresh_failure_network_unknown_and_explicit_failure(tmp_path: Path) -> None:
    release_body = _method_contents("releaseFinishedInventoryReservation")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;globalThis.createIdempotencyKey=()=> 'release-key';globalThis.confirm=()=>true;
function context(){{const messages=[];return {{messages,finishedReservationActionState:{{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}},orderItemForm:{{id:19}},orderItemOrder:{{id:7}},finishedReservationActionBlocked(){{const state=this.finishedReservationActionState;return Boolean(state.action||state.committed||state.outcomeUncertain);}},resetFinishedReservationActionState(){{this.finishedReservationActionState={{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}};}},showToast(message,danger=false){{messages.push({{message,danger}});}},errorMessage(error){{return error?.message||String(error);}}}};}}
const reservation={{id:71,lot_number:'LOT-71',reserved_stock_quantity:6}};
(async()=>{{
  globalThis.axios={{post:async()=>({{data:{{id:71,status:'released'}}}})}};const committed=context();committed.reloadFinishedInventoryState=async()=>false;const committedRelease=new AsyncFunction('reservation',{json.dumps(release_body, ensure_ascii=False)}).bind(committed);if(await committedRelease(reservation)!==true||!committed.finishedReservationActionState.committed||!committed.messages.some(row=>row.message.includes('已经取消')&&row.message.includes('核对库存状态'))) throw new Error('release refresh failure was not preserved');
  globalThis.axios={{post:async()=>{{throw new Error('network down');}}}};const unknown=context();unknown.reloadFinishedInventoryState=async()=>true;const unknownRelease=new AsyncFunction('reservation',{json.dumps(release_body, ensure_ascii=False)}).bind(unknown);if(await unknownRelease(reservation)!==false||!unknown.finishedReservationActionState.outcomeUncertain||!unknown.messages.some(row=>row.message.includes('结果暂不确定'))) throw new Error('release network unknown was not preserved');if(await unknownRelease(reservation)!==false) throw new Error('unknown release was allowed to retry');
  for(const status of [400,409,422]){{globalThis.axios={{post:async()=>{{const error=new Error('explicit failure');error.response={{status}};throw error;}}}};const explicit=context();explicit.reloadFinishedInventoryState=async()=>true;const explicitRelease=new AsyncFunction('reservation',{json.dumps(release_body, ensure_ascii=False)}).bind(explicit);if(await explicitRelease(reservation)!==false||explicit.finishedReservationActionBlocked()) throw new Error(`explicit release ${{status}} failure did not unlock retry`);}}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "finished-release-results.js")


def test_inventory_reconcile_clears_only_after_authoritative_reload(tmp_path: Path) -> None:
    body = _method_contents("refreshFinishedInventoryState")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
function context(ok){{const messages=[];return {{messages,finishedReservationActionState:{{action:'',committed:true,outcomeUncertain:false,itemId:19,lotId:31,reservationId:null,result:{{}}}},orderItemForm:{{id:19}},orderItemOrder:{{id:7}},async reloadFinishedInventoryState(){{return ok;}},resetFinishedReservationActionState(){{this.finishedReservationActionState={{action:'',committed:false,outcomeUncertain:false,itemId:null,lotId:null,reservationId:null,result:null}};}},showToast(message,danger=false){{messages.push({{message,danger}});}}}};}}
const refresh=new AsyncFunction({json.dumps(body, ensure_ascii=False)});
(async()=>{{const failed=context(false);if(await refresh.call(failed)!==false||!failed.finishedReservationActionState.committed||failed.finishedReservationActionState.action) throw new Error('failed reconcile incorrectly unlocked');const passed=context(true);if(await refresh.call(passed)!==true||passed.finishedReservationActionState.committed||!passed.messages.some(row=>row.message.includes('已核对'))) throw new Error('successful reconcile did not unlock');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "finished-reservation-reconcile.js")


def test_inventory_summary_ignores_late_response_from_previous_order(tmp_path: Path) -> None:
    body = _method_contents("refreshOrderItemInventorySummary")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;let finish;globalThis.axios={{get:()=>new Promise(resolve=>finish=resolve)}};
const oldForm={{id:19,finished_inventory_reserved_qty:0,production_required_qty:10,requisition_status:'未报料'}};const newForm={{id:29,finished_inventory_reserved_qty:4,production_required_qty:6,requisition_status:'未报料'}};
const vm={{modal:{{type:'orderItem'}},orderItemForm:oldForm,showToast(){{}},errorMessage(error){{return error?.message||String(error);}}}};const refresh=new AsyncFunction('itemId','orderId','expectedForm',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{const pending=refresh(19,7,oldForm);await Promise.resolve();vm.orderItemForm=newForm;finish({{data:{{items:[{{id:19,finished_inventory_reserved_qty:8,production_required_qty:2,requisition_status:'已抵扣'}}]}}}});if(await pending!==false||newForm.finished_inventory_reserved_qty!==4) throw new Error('late inventory summary overwrote the newer order item');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "finished-reservation-summary-race.js")

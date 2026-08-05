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
    assert node, "Node.js is required for BOM demand save validation"
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


def test_order_bom_demand_ui_locks_all_component_inputs() -> None:
    assert "orderBomDemandSaveState:{saving:false,outcomeUncertain:false,itemId:null,componentId:null,result:null}" in INDEX
    assert ':disabled="orderBomDemandSaveState.saving || orderBomDemandSaveState.outcomeUncertain"' in INDEX
    assert "保存中…" in INDEX
    assert "上次组件数量保存结果暂不确定" in INDEX
    load_orders = _method_body("loadOrders")
    assert "this.orderBomDemandSaveState?.outcomeUncertain" in load_orders
    assert "this.resetOrderBomDemandSaveState()" in load_orders


def test_order_bom_demand_save_is_single_flight_and_freezes_target(tmp_path: Path) -> None:
    body = _method_contents("saveOrderBomComponentDemand")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const finishes=[]; const calls=[]; let keyCalls=0; globalThis.createIdempotencyKey=()=>`bom-key-${{++keyCalls}}`;
globalThis.axios={{put:(url,payload)=>{{calls.push({{url,payload}});return new Promise(resolve=>finishes.push(resolve));}}}};
const messages=[];
const vm={{
  orderBomDemandSaveState:{{saving:false,outcomeUncertain:false,itemId:null,componentId:null,result:null}},
  showToast(message,danger=false){{messages.push({{message,danger}});}},
  async loadRequisition(){{return true;}},
  resetOrderBomDemandSaveState(){{this.orderBomDemandSaveState={{saving:false,outcomeUncertain:false,itemId:null,componentId:null,result:null}};}},
  errorMessage(error){{return error?.message || String(error);}},
}};
const save=new AsyncFunction('item','component',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{
  const item={{id:17,bom_components:[]}};
  const component={{id:31,effective_required_piece_quantity:3000,_demand_quantity:2700}};
  const first=save(item,component);
  const duplicatePromise=save({{id:99}},{{id:88,effective_required_piece_quantity:1,_demand_quantity:2}});
  await Promise.resolve();
  item.id=77;component.id=66;component._demand_quantity=999;
  if(calls.length!==1) throw new Error('duplicate BOM demand save was not blocked');
  if(calls[0].url!=='/api/orders/items/17/bom-components/31/demand') throw new Error('target IDs were not frozen');
  const payload=calls[0].payload;
  if(payload.required_piece_quantity!==2700 || payload.expected_required_piece_quantity!==3000 || payload.idempotency_key!=='bom-key-1') throw new Error('BOM demand payload was not frozen');
  finishes[0]({{data:{{components:[{{id:31,effective_required_piece_quantity:2700}}]}}}});
  const result=await first;
  const duplicate=await duplicatePromise;
  if(duplicate!==false) throw new Error('duplicate BOM demand save did not return false');
  if(!result || item.bom_components[0].effective_required_piece_quantity!==2700 || vm.orderBomDemandSaveState.saving) throw new Error('successful save did not update the current order component');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-bom-demand-single-flight.js")


def test_order_bom_demand_refresh_failure_remains_successful(tmp_path: Path) -> None:
    body = _method_contents("saveOrderBomComponentDemand")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.createIdempotencyKey=()=> 'stable-key';
globalThis.axios={{put:async()=>({{data:{{components:[{{id:5,effective_required_piece_quantity:2700}}]}}}})}};
const messages=[];
const vm={{orderBomDemandSaveState:{{saving:false,outcomeUncertain:false,itemId:null,componentId:null,result:null}},showToast(message,danger=false){{messages.push({{message,danger}});}},async loadRequisition(){{throw new Error('refresh down');}},resetOrderBomDemandSaveState(){{this.orderBomDemandSaveState={{saving:false,outcomeUncertain:false,itemId:null,componentId:null,result:null}};}},errorMessage(error){{return error?.message || String(error);}}}};
const save=new AsyncFunction('item','component',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{const item={{id:2,bom_components:[]}};const result=await save(item,{{id:5,effective_required_piece_quantity:3000,_demand_quantity:2700}});if(!result || item.bom_components[0].effective_required_piece_quantity!==2700) throw new Error('refresh failure erased successful save');if(!messages.some(row=>row.message.includes('已经保存')&&row.message.includes('手动刷新'))) throw new Error('refresh failure message was not truthful');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-bom-demand-refresh.js")


def test_order_bom_demand_unknown_result_blocks_retry_but_explicit_failure_does_not(tmp_path: Path) -> None:
    body = _method_contents("saveOrderBomComponentDemand")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;globalThis.createIdempotencyKey=()=> 'same-key';
function context(){{const messages=[];return {{messages,orderBomDemandSaveState:{{saving:false,outcomeUncertain:false,itemId:null,componentId:null,result:null}},showToast(message,danger=false){{messages.push({{message,danger}});}},async loadRequisition(){{return true;}},resetOrderBomDemandSaveState(){{this.orderBomDemandSaveState={{saving:false,outcomeUncertain:false,itemId:null,componentId:null,result:null}};}},errorMessage(error){{return error?.message || String(error);}}}};}}
const args=[{{id:2,bom_components:[]}},{{id:5,effective_required_piece_quantity:3000,_demand_quantity:2700}}];
(async()=>{{
  globalThis.axios={{put:async()=>{{throw new Error('network down');}}}};const unknown=context();const unknownSave=new AsyncFunction('item','component',{json.dumps(body, ensure_ascii=False)}).bind(unknown);if(await unknownSave(...args)!==false || !unknown.orderBomDemandSaveState.outcomeUncertain || !unknown.messages.some(row=>row.message.includes('结果暂不确定'))) throw new Error('network uncertainty was not isolated');if(await unknownSave(...args)!==false) throw new Error('uncertain action was allowed to retry');
  globalThis.axios={{put:async()=>{{const error=new Error('conflict');error.response={{status:409}};throw error;}}}};const explicit=context();const explicitSave=new AsyncFunction('item','component',{json.dumps(body, ensure_ascii=False)}).bind(explicit);if(await explicitSave(...args)!==false || explicit.orderBomDemandSaveState.saving || explicit.orderBomDemandSaveState.outcomeUncertain) throw new Error('explicit failure did not remain retryable');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "order-bom-demand-errors.js")

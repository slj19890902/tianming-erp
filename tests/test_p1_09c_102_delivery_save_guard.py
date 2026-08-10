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
    assert node, "Node.js is required for delivery save behavior validation"
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


def test_delivery_save_ui_has_local_busy_state_and_resets_on_open() -> None:
    assert 'deliverySaveState:{saving:false,committed:false,outcomeUncertain:false,result:null}' in INDEX
    assert ':disabled="deliverySaveState.saving"' in INDEX
    assert "modal?.type==='delivery' && deliverySaveState.saving" in INDEX
    assert "正在保存草稿…" in INDEX
    assert "送货草稿正在保存，请稍候" in _method_body("closeModal")
    assert "this.deliverySaveState = {saving:false,committed:false,outcomeUncertain:false,result:null};" in _method_body("openDelivery")
    assert "this.deliverySaveState = {saving:false,committed:false,outcomeUncertain:false,result:null};" in _method_body("editDelivery")


def test_delivery_save_blocks_duplicate_and_freezes_payload(tmp_path: Path) -> None:
    body = _method_contents("saveCurrentDeliveryDraft")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const requests=[]; let finish;
globalThis.axios={{post:(url,payload)=>{{requests.push({{url,payload}});return new Promise(resolve=>finish=resolve);}},put:()=>{{throw new Error('unexpected put')}}}};
const vm={{
  deliverySaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},
  deliveryForm:{{editingId:null,delivery_number:'',pick_task:null,saved_signature:''}},
  modal:{{type:'delivery',title:'新增送货单'}}, pages:{{deliveries:3}},
  deliveryFormSignature(){{return 'saved-signature';}}, async loadDeliveries(){{return true;}},
  invalidated:[],invalidateDeliveryListDetail(id){{this.invalidated.push(Number(id));return true;}},
}};
const save=new AsyncFunction('editingId','deliveryPayload',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{
  const source={{customer_id:7,delivery_date:'2026-08-06',vehicle_number:'苏E1',source_mode:'unordered_finished',items:[{{source_type:'unordered_finished',product_id:9,delivered_quantity:3,allocations:[{{inventory_lot_id:55,quantity:3}}]}}]}};
  const first=save(null,source);
  const duplicate=await save(null,source);
  source.items[0].delivered_quantity=99; source.items[0].allocations[0].quantity=99;
  if(!duplicate?._in_flight || requests.length!==1) throw new Error('duplicate delivery save was not blocked');
  if(requests[0].payload.items[0].delivered_quantity!==3 || requests[0].payload.items[0].allocations[0].quantity!==3) throw new Error('delivery payload was not frozen');
  finish({{data:{{id:81,delivery_number:'TH000081',pick_task:null}}}});
  const result=await first;
  if(result.id!==81 || vm.deliveryForm.editingId!==81 || vm.deliverySaveState.saving || !vm.invalidated.includes(81)) throw new Error('successful save did not commit, invalidate and unlock');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "delivery-save-duplicate.js")


def test_delivery_save_refresh_failure_is_still_committed(tmp_path: Path) -> None:
    body = _method_contents("saveCurrentDeliveryDraft")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.axios={{post:async()=>({{data:{{id:82,delivery_number:'TH000082'}}}})}};
const vm={{deliverySaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},deliveryForm:{{editingId:null,delivery_number:'',pick_task:null,saved_signature:''}},modal:{{type:'delivery'}},pages:{{deliveries:2}},invalidated:[],deliveryFormSignature(){{return 'saved';}},invalidateDeliveryListDetail(id){{this.invalidated.push(Number(id));return true;}},async loadDeliveries(){{throw new Error('refresh down');}}}};
const save=new AsyncFunction('editingId','deliveryPayload',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{const result=await save(null,{{customer_id:7,delivery_date:'2026-08-06',items:[]}});if(!result._refresh_failed || vm.deliveryForm.editingId!==82 || vm.modal?.type!=='delivery' || vm.deliverySaveState.saving || !vm.invalidated.includes(82)) throw new Error('refresh failure erased committed delivery or kept stale detail');}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "delivery-save-refresh.js")


def test_delivery_save_distinguishes_unknown_and_explicit_failures(tmp_path: Path) -> None:
    body = _method_contents("saveCurrentDeliveryDraft")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const saveBody={json.dumps(body, ensure_ascii=False)};
function context(){{return {{deliverySaveState:{{saving:false,committed:false,outcomeUncertain:false,result:null}},deliveryForm:{{editingId:null,delivery_number:'',pick_task:null,saved_signature:''}},modal:{{type:'delivery'}},pages:{{deliveries:1}},deliveryFormSignature(){{return 'saved';}},invalidateDeliveryListDetail(){{return true;}},async loadDeliveries(){{return true;}}}};}}
(async()=>{{
  globalThis.axios={{post:async()=>{{throw new Error('network down');}}}};
  const unknown=context(); const unknownSave=new AsyncFunction('editingId','deliveryPayload',saveBody).bind(unknown);
  let unknownError=null; try{{await unknownSave(null,{{customer_id:7,delivery_date:'2026-08-06',items:[]}});}}catch(error){{unknownError=error;}}
  if(!unknownError?._deliverySaveOutcomeUncertain || unknown.modal!==null || !unknown.deliverySaveState.outcomeUncertain) throw new Error('network outcome was not isolated');
  globalThis.axios={{post:async()=>{{const error=new Error('conflict');error.response={{status:409}};throw error;}}}};
  const explicit=context(); const explicitSave=new AsyncFunction('editingId','deliveryPayload',saveBody).bind(explicit);
  let explicitError=null; try{{await explicitSave(null,{{customer_id:7,delivery_date:'2026-08-06',items:[]}});}}catch(error){{explicitError=error;}}
  if(explicitError?.response?.status!==409 || explicit.modal?.type!=='delivery' || explicit.deliverySaveState.saving || explicit.deliverySaveState.outcomeUncertain) throw new Error('explicit failure did not remain retryable');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "delivery-save-errors.js")


def test_save_modal_routes_delivery_through_guard_and_truthful_messages() -> None:
    body = _method_body("saveModal")
    assert "送货草稿正在保存，请勿重复点击" in body
    assert "const savedDelivery = await this.saveCurrentDeliveryDraft(" in body
    assert "送货草稿已保存；列表刷新失败" in body
    assert "_deliverySaveOutcomeUncertain" in body
    assert "送货草稿保存请求连接中断，结果暂不确定" in body
    assert 'if (!deliveryUsesLocalSaveState) this.loading = true;' in body
    assert 'if (!deliveryUsesLocalSaveState) this.loading = false;' in body

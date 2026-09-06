from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static/incoming.html").read_text(encoding="utf-8")


def _method(start: str, end: str, source: str = INDEX) -> str:
    return source.split(start, 1)[1].split(end, 1)[0].rsplit("}", 1)[0]


def _node(script: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the receipt price recovery checks"
    target = tmp_path / "receipt-price-recovery.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("role,has_action", [("admin", True), ("workshop", False), ("boss", False)])
def test_price_error_action_matches_existing_admin_only_material_write(role: str, has_action: bool) -> None:
    class ReceiptError(Exception):
        code = "SUPPLIER_RECEIPT_MASTER_PRICE_INVALID"
        status_code = 422

    source = ast.parse((ROOT / "app/api/incoming.py").read_text(encoding="utf-8"))
    helpers = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in {"_receipt_price_recovery", "_raise_receipt_error"}]
    namespace = {"User": SimpleNamespace, "IncomingReceiptError": ReceiptError, "HTTPException": HTTPException}
    exec(compile(ast.Module(body=helpers, type_ignores=[]), "receipt_error_helpers", "exec"), namespace)
    with pytest.raises(HTTPException) as result:
        namespace["_raise_receipt_error"](ReceiptError("供应商材质缺少有效采购价格"), SimpleNamespace(role=role))
    assert result.value.status_code == 422
    detail = result.value.detail
    assert detail["code"] == ReceiptError.code
    assert detail["message"] == "供应商材质缺少有效采购价格"
    assert ("action_url" in detail) is has_action
    if has_action:
        assert detail["action_url"] == "/?page=products&subpage=materials&incoming_price_return=1"
    else:
        assert "请联系管理员" in detail["action_hint"]
    assert namespace["_receipt_price_recovery"]("ORDER_ITEM_RECEIPT_BLOCKED", SimpleNamespace(role=role)) == {}


def test_desktop_price_repair_retry_keeps_quantity_and_idempotency_key(tmp_path: Path) -> None:
    receive = _method("async receiveIncoming(row) {", "async acceptShortIncoming(row) {")
    recovery = _method('recordIncomingPriceError(row, error, idempotencyKey="") {', "async batchReceiveIncoming() {")
    route = _method("applyInitialMaterialPriceRoute() {", "returnToIncomingPriceRequest() {")
    _node(f"""
const assert=require('node:assert/strict');
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let sequence=0;global.createIdempotencyKey=()=>`receipt-${{++sequence}}`;
global.window={{location:{{search:'?page=products&subpage=materials&incoming_price_return=1'}}}};
const calls=[];let blocked=true;
global.axios={{put:async(url,payload)=>{{calls.push({{url,payload:{{...payload}}}});if(blocked)throw {{response:{{status:422,data:{{detail:{{code:'SUPPLIER_RECEIPT_MASTER_PRICE_INVALID',message:'缺少供应商报价'}}}}}}}};return {{data:{{material_status:'received'}}}};}}}};
const row={{item_id:'sr7',source_type:'stock_replenishment',incoming_quantity:17}};
const vm={{user:{{id:1}},authGeneration:1,incomingPendingAppliedPage:1,incomingReceiveAttempts:{{}},incomingPriceRecovery:{{}},
ensureAutomaticPurchaseReceiptFact:async()=>{{}},incomingPayload:r=>({{item_id:r.item_id,received_quantity:r.incoming_quantity}}),
showToast(){{}},refreshIncomingAfterWrite:async()=>true,errorMessage:e=>e?.response?.data?.detail?.message||String(e),
activePage:'products',canAdmin:true,pageAllowed:()=>true,productTab:'products'}};
vm.recordIncomingPriceError=new Function('row','error','idempotencyKey',{json.dumps(recovery)}).bind(vm);
vm.receiveIncoming=new AsyncFunction('row',{json.dumps(receive)}).bind(vm);
vm.applyInitialMaterialPriceRoute=new Function({json.dumps(route)}).bind(vm);
(async()=>{{
 await vm.receiveIncoming(row);
 assert.equal(calls.length,1);assert.equal(vm.incomingReceiveAttempts.sr7.saving,false);
 assert.equal(vm.incomingPriceRecovery.sr7.draft.incoming_quantity,17);
 assert.equal(vm.incomingPriceRecovery.sr7.idempotencyKey,calls[0].payload.idempotency_key);
 vm.applyInitialMaterialPriceRoute();assert.equal(vm.productTab,'materials');assert.equal(vm.incomingPriceReturn,true);
 assert.equal(calls.length,1,'opening quotation route must not post a receipt');
 blocked=false;await vm.receiveIncoming(row);
 assert.equal(calls.length,2);assert.equal(calls[1].payload.received_quantity,17);
 assert.equal(calls[0].payload.idempotency_key,calls[1].payload.idempotency_key);
 assert.equal(vm.incomingPriceRecovery.sr7,undefined);
 vm.activePage='incoming';vm.productTab='products';vm.applyInitialMaterialPriceRoute();assert.equal(vm.productTab,'products');
}})().catch(e=>{{console.error(e);process.exit(1);}});
""", tmp_path)


def test_desktop_batch_keeps_failed_price_line_draft_after_successful_lines_refresh(tmp_path: Path) -> None:
    batch = _method("async batchReceiveIncoming() {", "async receiveIncoming(row) {")
    recovery = _method('recordIncomingPriceError(row, error, idempotencyKey="") {', "async batchReceiveIncoming() {")
    pending = _method("applyIncomingPendingResponse(data, {requestedPage=1, clearSelection=false}={}) {", "async loadIncomingPendingPage(")
    _node(f"""
const assert=require('node:assert/strict');const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let sequence=0;global.createIdempotencyKey=()=>`batch-${{++sequence}}`;global.confirm=()=>true;
let sent;global.axios={{put:async(url,payload)=>{{sent=payload;return {{data:{{succeeded:1,failed:1,results:[{{item_id:'sr1',success:true}},{{item_id:'sr2',success:false,code:'SUPPLIER_RECEIPT_MASTER_PRICE_INVALID',message:'缺价'}}]}}}};}}}};
const vm={{user:{{id:1}},authGeneration:1,incomingPendingAppliedPage:1,pages:{{incomingPending:1}},incomingPriceRecovery:{{}},incomingReceiveAttempts:{{}},incomingSelected:{{sr1:true,sr2:true}},incomingBatchReceiveAttempt:null,
incomingPending:[{{item_id:'sr1',source_type:'stock_replenishment',incoming_quantity:8}},{{item_id:'sr2',source_type:'stock_replenishment',incoming_quantity:13}}],
canReceiveIncoming:()=>true,ensureAutomaticPurchaseReceiptFact:async()=>{{}},incomingPayload:r=>({{item_id:r.item_id,received_quantity:r.incoming_quantity}}),errorMessage:e=>e.message,showToast(){{}},showIncomingNextStepGuide(){{}}}};
vm.recordIncomingPriceError=new Function('row','error','idempotencyKey',{json.dumps(recovery)}).bind(vm);
vm.applyIncomingPendingResponse=new Function('data','{{requestedPage=1,clearSelection=false}}={{}}',{json.dumps(pending)}).bind(vm);
vm.refreshIncomingAfterWrite=async()=>{{vm.applyIncomingPendingResponse({{items:[{{item_id:'sr2',remaining_quantity:99}}],page:1}});return true;}};
vm.batchReceiveIncoming=new AsyncFunction({json.dumps(batch)}).bind(vm);
(async()=>{{await vm.batchReceiveIncoming();assert.equal(vm.incomingPending.length,1);assert.equal(vm.incomingPending[0].incoming_quantity,13);assert.equal(vm.incomingPending[0]._receipt_price_retry_key,sent.items[1].idempotency_key);assert.equal(vm.incomingPriceRecovery.sr2.message,'缺价');assert.equal(vm.incomingBatchReceiveAttempt,null);}})().catch(e=>{{console.error(e);process.exit(1);}});
""", tmp_path)


def test_mobile_price_repair_retains_draft_and_has_admin_only_link(tmp_path: Path) -> None:
    receive = _method("async function receive(itemId) {", "async function acceptShortNow(itemId) {", MOBILE)
    recovery = _method("function recordReceiptPriceError(item, error) {", "async function receive(itemId) {", MOBILE)
    recovery_markup = MOBILE.split("      const priceRecovery =", 1)[1].split("      const expanded =", 1)[0]
    _node(f"""
const assert=require('node:assert/strict');const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const row={{item_id:'sr2',incoming_quantity:11,planned_quantity:11,purpose_status:'legacy_unset'}};
global.state={{busyItemIds:new Set(),receiveIdempotencyKeys:new Map(),locations:[]}};global.findItem=()=>row;
global.receiptExecutionIssue=()=>'';global.createIdempotencyKey=()=> 'mobile-stable';global.render=()=>{{}};
global.ensureAutomaticReceiptFact=async()=>{{}};global.showToast=()=>{{}};global.toChineseMessage=e=>e.message;
global.refreshAfterIncomingWrite=async()=>true;
global.recordReceiptPriceError=new Function('item','error',{json.dumps(recovery)});
const calls=[];let blocked=true;global.api=async(url,options)=>{{calls.push(JSON.parse(options.body));if(blocked)throw {{message:'缺价',detail:{{code:'SUPPLIER_RECEIPT_MASTER_PRICE_INVALID',message:'缺价'}}}};return {{material_status:'received'}};}};
const receive=new AsyncFunction('itemId',{json.dumps(receive)});
const markup=new Function('isPending','item','state','escapeHtml','const priceRecovery='+{json.dumps(recovery_markup)}+'return priceRecovery;');
(async()=>{{await receive('sr2');assert.equal(row.incoming_quantity,11);assert.equal(row._receipt_price_error,'缺价');assert.equal(state.receiveIdempotencyKeys.get('sr2'),'mobile-stable');
 const admin=markup(true,row,{{user:{{role:'admin'}}}},String);const clerk=markup(true,row,{{user:{{role:'workshop'}}}},String);
 assert.ok(admin.includes('target="_blank"'));assert.ok(admin.includes('rel="noopener"'));assert.ok(admin.includes('前往供应商材质报价'));assert.ok(!clerk.includes('<a '));assert.ok(clerk.includes('请联系管理员'));
 blocked=false;await receive('sr2');assert.equal(calls.length,2);assert.equal(calls[0].idempotency_key,calls[1].idempotency_key);assert.equal(calls[1].received_quantity,11);
}})().catch(e=>{{console.error(e);process.exit(1);}});
""", tmp_path)

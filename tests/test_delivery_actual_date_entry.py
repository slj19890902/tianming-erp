import json
import re

from fastapi.testclient import TestClient

from tests.test_p1_46_common_box_final_layout import INDEX, _method, _run_node
from test_phase8_finance import (
    _create_statement,
    _login,
    _mark_statement_confirmed,
    finance_api_app,
)


def _runtime() -> str:
    params, body = _method("correctDeliveryActualDate")
    return f"""
const assert=require('node:assert/strict');
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const calls=[],toasts=[];
global.prompt=()=> '2026-10-12';
global.confirm=()=>true;
global.createIdempotencyKey=()=> 'date-entry-test-key';
global.axios={{put:async (url,payload)=>{{calls.push({{url,payload}});return {{data:{{delivery_date:payload.actual_delivery_date,version:8}}}};}}}};
const vm={{canDelivery:true,canAdjustReconciliationPeriod:true,
 deliveryOperationState:{{action:''}},receiptOperationState:{{action:''}},deliveryInlineEdit:null,
 invalidateDeliveryListDetail(){{}},loadDeliveries:async()=>{{}},loadFinance:async()=>{{}},loadKpi:async()=>{{}},
 showToast:(text,error)=>toasts.push({{text,error}}),errorMessage:error=>error.response.data.detail.message}};
vm.correctDeliveryActualDate=new AsyncFunction({json.dumps(params)},{json.dumps(body)}).bind(vm);
const row={{id:192,delivery_number:'GT-20261021-001',status:'dispatched',return_receipt_status:'confirmed',
 delivery_date:'2026-10-21',version:7,total_quantity:100,total_amount:'480.00'}};
"""


def test_more_menu_reaches_date_correction_with_confirmed_receipt(tmp_path):
    button = re.search(r'<button\b[^>]*data-action="correct-delivery-actual-date"[^>]*>', INDEX)
    assert button, "normal delivery More menu must expose controlled date correction"
    attrs = dict(re.findall(r'([:@\w-]+)="([^"]*)"', button.group()))
    assert attrs["@click"] == "correctDeliveryActualDate(row)"
    _run_node(tmp_path, _runtime() + f"""
const visible=new Function('vm','row',{json.dumps('with(vm){return (' + attrs['v-if'] + ');}')});
const disabled=new Function('vm',{json.dumps('with(vm){return (' + attrs[':disabled'] + ');}')});
assert.equal(visible(vm,row),true,'confirmed receipt must not require cancellation');
assert.equal(disabled(vm),false);
for(const key of ['canDelivery','canAdjustReconciliationPeriod']){{
 vm[key]=false;assert.equal(visible(vm,row),false);vm[key]=true;
}}
row.status='voided';assert.equal(visible(vm,row),false);row.status='dispatched';
row.status='pending';assert.equal(visible(vm,row),true);row.status='dispatched';
for(const state of ['deliveryOperationState','receiptOperationState']){{
 vm[state].action='busy';assert.equal(disabled(vm),true);vm[state].action='';
}}
vm.deliveryInlineEdit={{}};assert.equal(disabled(vm),true);vm.deliveryInlineEdit=null;
(async()=>{{
 const ok=await vm.correctDeliveryActualDate(row);
 assert.equal(ok,true);assert.equal(calls.length,1);
 assert.deepEqual(calls[0],{{url:'/api/deliveries/192/actual-date',payload:{{
  actual_delivery_date:'2026-10-12',expected_version:7,idempotency_key:'date-entry-test-key'}}}});
 assert.equal(row.return_receipt_status,'confirmed');assert.equal(row.total_quantity,100);
 assert.equal(row.total_amount,'480.00');assert.equal(row.delivery_date,'2026-10-12');
 assert.equal(vm.deliveryOperationState.action,'');
}})().catch(error=>{{console.error(error);process.exitCode=1;}});
""")


def test_date_method_guards_and_finance_rejection_do_not_mutate_row(tmp_path):
    _run_node(tmp_path, _runtime() + """
(async()=>{
 for(const key of ['canDelivery','canAdjustReconciliationPeriod']){
  vm[key]=false;assert.equal(await vm.correctDeliveryActualDate(row),false);vm[key]=true;
 }
 row.status='voided';assert.equal(await vm.correctDeliveryActualDate(row),false);row.status='dispatched';
 for(const state of ['deliveryOperationState','receiptOperationState']){
  vm[state].action='busy';assert.equal(await vm.correctDeliveryActualDate(row),false);vm[state].action='';
 }
 vm.deliveryInlineEdit={};assert.equal(await vm.correctDeliveryActualDate(row),false);vm.deliveryInlineEdit=null;
 assert.equal(calls.length,0,'guarded actions must not reach the API');
 const before=JSON.stringify(row);
 axios.put=async(url,payload)=>{calls.push({url,payload});throw {response:{status:409,data:{detail:{
  code:'delivery_actual_date_locked',message:'该送货单已进入对账/开票，不能直接更正实际日期'}}}};};
 assert.equal(await vm.correctDeliveryActualDate(row),false);
 assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/deliveries/192/actual-date');
 assert.equal(JSON.stringify(row),before);
 assert.equal(vm.deliveryOperationState.action,'');
 assert.equal(toasts.at(-1).error,true);assert.match(toasts.at(-1).text,/开票/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_date_endpoint_remains_locked_after_invoice(finance_api_app):
    # The shared fixture creates only tmp_path/finance.sqlite3, never the ERP DB.
    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _mark_statement_confirmed(session_factory, _create_statement(client))
        invoice = client.post("/api/finance/invoices", json={
            "statement_id": statement["id"],
            "invoice_number": "INV-DATE-ENTRY-001",
            "invoice_date": "2026-06-14",
            "invoice_amount": str(statement["total_receivable"]),
            "expected_version": statement["version"],
            "expected_ledger_version": 1,
            "idempotency_key": "invoice-date-entry-lock-001",
        })
        assert invoice.status_code == 201, invoice.text
        _login(client, "admin")
        before = client.get("/api/deliveries/1").json()
        rejected = client.put("/api/deliveries/1/actual-date", json={
            "actual_delivery_date": "2026-06-10",
            "expected_version": before["version"],
            "idempotency_key": "date-entry-after-invoice-001",
        })
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "delivery_actual_date_locked"
        after = client.get("/api/deliveries/1").json()
        for field in ("delivery_date", "created_at", "version", "total_quantity", "total_amount", "return_receipt_status", "items"):
            assert after[field] == before[field], field

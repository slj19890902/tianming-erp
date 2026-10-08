from pathlib import Path
import json
from test_p1_09c_106_requisition_void_action_guard import _method_body, _run_node


def test_real_vue_proxy_releases_completed_single_and_batch_receipts(tmp_path):
    methods=",".join(_method_body(n).strip().rstrip(",") for n in ["receiveIncoming","batchReceiveIncoming"])
    vue=Path(__file__).resolve().parents[1]/"frontend-v2/node_modules/vue/dist/vue.cjs.prod.js"
    script="const assert=require('node:assert/strict');const {reactive}=require("+json.dumps(str(vue))+");const methods={"+methods+"};"+r"""
let writes=[],seq=0;const createIdempotencyKey=()=>`key-${++seq}`;
const axios={put:async(url,payload)=>{writes.push({url,payload:JSON.parse(JSON.stringify(payload))});return {data:{material_status:'pending',remaining_quantity:3,succeeded:1,failed:0,results:[{success:true,item_id:'so9',item:{material_status:'pending'}}]}};}};
function context(){return reactive({...methods,user:{id:7},authGeneration:1,incomingPendingAppliedPage:1,incomingReceiveAttempts:{},incomingBatchReceiveAttempt:null,incomingSelected:{},incomingPending:[],incomingPriceRecovery:{},
canReceiveIncoming:()=>true,ensureAutomaticPurchaseReceiptFact:async()=>{},incomingPayload:row=>({item_id:row.item_id,received_quantity:row.incoming_quantity}),refreshIncomingAfterWrite:async()=>true,showIncomingNextStepGuide(){},showToast(){},errorMessage:e=>e.message});}
(async()=>{
const c=context();await c.receiveIncoming({item_id:'so9',incoming_quantity:5});assert.equal(c.incomingReceiveAttempts.so9,null,'successful reactive single receipt must release attempt');
await c.receiveIncoming({item_id:'so9',incoming_quantity:3});assert.equal(writes.length,2,'next click must save the next batch, not merely refresh the previous one');assert.equal(writes[1].payload.received_quantity,3);assert.notEqual(writes[0].payload.idempotency_key,writes[1].payload.idempotency_key);
writes=[];const b=context();b.incomingPending=[{item_id:'so9',incoming_quantity:5}];b.incomingSelected={so9:true};await b.batchReceiveIncoming();assert.equal(b.incomingBatchReceiveAttempt,null,'successful reactive batch must release attempt');
b.incomingPending[0].incoming_quantity=3;b.incomingSelected={so9:true};await b.batchReceiveIncoming();assert.equal(writes.length,2);assert.equal(writes[1].payload.items[0].received_quantity,3);
const rejected=context();rejected.ensureAutomaticPurchaseReceiptFact=async()=>{throw Error('price gate');};await rejected.receiveIncoming({item_id:'so9',incoming_quantity:1});assert.equal(rejected.incomingReceiveAttempts.so9,null,'failed preparation must release the same reactive attempt');
const uncertain=context();uncertain.refreshIncomingAfterWrite=async()=>false;await uncertain.receiveIncoming({item_id:'so9',incoming_quantity:1});assert.equal(uncertain.incomingReceiveAttempts.so9.committed,true,'failed readback must retain committed protection');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    _run_node(script,tmp_path,"actual-vue-receipt-state.cjs")

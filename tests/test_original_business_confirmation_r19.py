from test_p1_09c_106_requisition_void_action_guard import _method_body, _run_node, INDEX


def test_confirmation_requires_explicit_accept_and_same_identity(tmp_path):
    methods=",".join(_method_body(n).strip().rstrip(",") for n in ["confirmOriginalBusinessAction","finishOriginalBusinessConfirmation"])
    script="const assert=require('node:assert/strict');const document={activeElement:{isConnected:true,focus(){}}};const methods={"+methods+"};"+r"""
function context(){return {...methods,user:{id:7},authGeneration:3,originalBusinessConfirmation:{visible:false},$refs:{originalBusinessConfirmation:{open:false,showModal(){this.open=true;},close(){this.open=false;}}},$nextTick:async()=>{},showToast(){}};}
(async()=>{
for(const accept of [false,true]){
 const c=context(),p=c.confirmOriginalBusinessAction('message',{title:'title',confirmLabel:'确认撤销'});await Promise.resolve();
 assert.equal(c.originalBusinessConfirmation.visible,true);assert.equal(await c.confirmOriginalBusinessAction('duplicate',{}),false);
 c.finishOriginalBusinessConfirmation(accept);assert.equal(await p,accept);assert.equal(c.originalBusinessConfirmation.visible,false);
 c.finishOriginalBusinessConfirmation(true);assert.equal(c.originalBusinessConfirmation.visible,false);
}
for(const change of [c=>c.authGeneration++,c=>c.user={id:8},c=>c.user=null]){
 const c=context(),p=c.confirmOriginalBusinessAction('message',{});await Promise.resolve();change(c);c.finishOriginalBusinessConfirmation(true);assert.equal(await p,false);
}
const broken=context();broken.$refs.originalBusinessConfirmation.showModal=()=>{throw Error('unavailable');};assert.equal(await broken.confirmOriginalBusinessAction('message',{}),false);
const anonymous=context();anonymous.user=null;assert.equal(await anonymous.confirmOriginalBusinessAction('message',{}),false);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    _run_node(script,tmp_path,"confirmation-identity-and-cancel.cjs")


def test_receipt_reversal_waits_for_confirmation_locks_and_freezes_target(tmp_path):
    method=_method_body("revertIncoming").strip().rstrip(",")
    script="const assert=require('node:assert/strict');let writes=[];const axios={put:async(url,payload)=>{writes.push({url,payload});}};const createIdempotencyKey=()=> 'r19-test-idempotency';const methods={"+method+"};"+r"""
function context(){return {...methods,canAdmin:true,incomingRevertBusy:false,incomingTab:'history',incomingPendingAppliedPage:1,showToast(){},errorMessage:e=>e.message,loadIncomingPendingPage:async()=>{},loadIncomingHistory:async()=>{},loadKpi:async()=>{}};}
(async()=>{
for(const answer of [false,true]){
 writes=[];const c=context();let choose;c.confirmOriginalBusinessAction=()=>new Promise(r=>choose=r);
 const row={receipt_item_id:18,item_id:'so8'},p=c.revertIncoming(row);assert.equal(writes.length,0);
 assert.equal(await c.revertIncoming({receipt_item_id:19,item_id:'so9'}),false);
 row.receipt_item_id=999;choose(answer);await p;assert.equal(writes.length,answer?1:0);assert.equal(c.incomingRevertBusy,false);
 if(answer){assert.equal(writes[0].url,'/api/incoming/receipt-items/18/revert');assert.equal(writes[0].payload.idempotency_key,'r19-test-idempotency');}
}
const denied=context();denied.canAdmin=false;denied.confirmOriginalBusinessAction=()=>{throw Error('must not ask');};assert.equal(await denied.revertIncoming({receipt_item_id:18}),false);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    _run_node(script,tmp_path,"receipt-confirm-single-flight.cjs")


def test_cancel_esc_close_and_backdrop_share_nonexecuting_exit():
    dialog=INDEX.split('<dialog v-if="originalBusinessConfirmation.visible"',1)[1].split('</dialog>',1)[0]
    assert '@cancel.prevent="finishOriginalBusinessConfirmation(false)"' in dialog
    assert '@close="finishOriginalBusinessConfirmation(false)"' in dialog
    assert '@click.self="finishOriginalBusinessConfirmation(false)"' in dialog
    assert 'autofocus @click="finishOriginalBusinessConfirmation(false)"' in dialog
    assert dialog.count('finishOriginalBusinessConfirmation(true)')==1
    for name in ['revertIncoming','voidSupplierOrder','cancelRequisition','voidReportedCompositeRequisition','voidReportedCompositeLine','voidReportedItem','voidReportedReplenishment']:
        body=_method_body(name)
        assert body.count('confirmOriginalBusinessAction(')==1
        assert 'confirm(' not in body and 'prompt(' not in body

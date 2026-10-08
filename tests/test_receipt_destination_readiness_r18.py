from test_p1_91v_incoming_receive_409_busy import _method_body, _run_node


def test_only_selected_receipt_destinations_gate_submission(tmp_path):
    body = _method_body("incomingReceiptExecutionIssue(row) {", "toggleAllIncoming(checked) {")
    script = "const assert=require('node:assert/strict');const vm={incomingSurplusDispositionRequired:()=>false,incomingPurposeProjection:r=>r.projection,incomingReceiptExecutionIssue(row){" + body + "}};" + r"""
const reserve={projection:{orderSheets:0,reserveSheets:3},surplus_disposition:'semi_finished_reserve',receipt_execution_ready:false,finished_location_ready:false,reserve_location_ready:true,finished_location_issue:'finished unavailable',purpose_issue:'finished unavailable'};
assert.equal(vm.incomingReceiptExecutionIssue(reserve),'');
assert.equal(vm.incomingReceiptExecutionIssue({...reserve,reserve_location_ready:false,reserve_location_issue:'reserve unavailable'}),'reserve unavailable');
assert.equal(vm.incomingReceiptExecutionIssue({...reserve,projection:{orderSheets:1,reserveSheets:3}}),'finished unavailable');
assert.notEqual(vm.incomingReceiptExecutionIssue({...reserve,receipt_location_ready:false}),'');
assert.equal(vm.incomingReceiptExecutionIssue({...reserve,projection:{orderSheets:3,reserveSheets:0},finished_location_ready:true,reserve_location_ready:false}),'');
assert.notEqual(vm.incomingReceiptExecutionIssue({projection:{orderSheets:1,reserveSheets:0},receipt_execution_ready:false}),'');
assert.equal(vm.incomingReceiptExecutionIssue({...reserve,finished_location_ready:true,receipt_execution_ready:true}),'');
"""
    _run_node(script,tmp_path,"receipt-selected-destinations.cjs")


def test_receipt_next_step_uses_actual_allocation_and_preserves_legacy_guidance(tmp_path):
    show = _method_body("showIncomingNextStepGuide({count=1,pendingBalance=false,receipts=[]}={}) {", "dismissIncomingNextStepGuide() {")
    message = _method_body("incomingNextStepGuideMessage() {", "productionNextStepGuideTitle() {")
    navigate = _method_body("async goToDeliveryFromIncomingGuide() {", "showProductionNextStepGuide(")
    script = "const assert=require('node:assert/strict');const vm={show({count=1,pendingBalance=false,receipts=[]}={}){"+show+"},message(){"+message+"},async navigate(){"+navigate+"}};" + r"""
(async()=>{
const reserve={purpose_allocation:{theoretical_finished_delta:0,reserve_sheet_delta:3}};
const finished={purpose_allocation:{theoretical_finished_delta:5,reserve_sheet_delta:0}};
vm.show({receipts:[reserve]});
assert.equal(vm.incomingNextStepGuide.hasFinished,false);assert.equal(vm.incomingNextStepGuide.hasReserve,true);
assert.match(vm.message(),/本次未形成成品/);assert.equal(await vm.navigate(),false);
vm.show({receipts:[finished,reserve],count:2,pendingBalance:true});
assert.equal(vm.incomingNextStepGuide.hasFinished,true);assert.match(vm.message(),/备库片料/);assert.match(vm.message(),/待入库/);
vm.show({receipts:[finished]});assert.equal(vm.incomingNextStepGuide.hasReserve,false);assert.match(vm.message(),/可送货/);
vm.show({receipts:[{}]});assert.equal(vm.incomingNextStepGuide.hasFinished,true);
vm.show({receipts:[{purpose_allocation:{theoretical_finished_delta:0,reserve_sheet_delta:0}}]});assert.equal(vm.incomingNextStepGuide.hasFinished,false);assert.match(vm.message(),/核对用途分配/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    _run_node(script,tmp_path,"receipt-result-guidance.cjs")

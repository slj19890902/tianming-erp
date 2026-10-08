from test_p1_09c_106_requisition_void_action_guard import _method_body, _run_node


def test_changed_receipt_quantity_never_reuses_another_quantitys_finished_prediction(tmp_path):
    method = _method_body('incomingPurposeProjection').strip().rstrip(',')
    script = "const assert=require('node:assert/strict');const methods={" + method + "};" + r'''
const row={purpose_status:'frozen',incoming_quantity:11,remaining_order_purpose_sheet_qty:8,
 expected_order_purpose_sheet_qty:8,expected_finished_output_qty:8,
 finished_disposition_expected_order_purpose_sheet_qty:11,finished_disposition_expected_finished_output_qty:11,
 semi_finished_reserve_expected_order_purpose_sheet_qty:8,semi_finished_reserve_expected_finished_output_qty:8,
 surplus_disposition:'semi_finished_reserve'};
assert.deepEqual(methods.incomingPurposeProjection(row),{orderSheets:8,reserveSheets:3,finishedOutput:8});
row.incoming_quantity=5;row.surplus_disposition='';
assert.deepEqual(methods.incomingPurposeProjection(row),{orderSheets:5,reserveSheets:0,finishedOutput:null},'five sheets must not display the old eight-box result');
row.incoming_quantity=8;assert.equal(methods.incomingPurposeProjection(row).finishedOutput,8);
row.incoming_quantity=10;row.surplus_disposition='finished';
assert.equal(methods.incomingPurposeProjection(row).finishedOutput,null,'ten finished-purpose sheets must not display the eleven-sheet result');
row.incoming_quantity=11;assert.equal(methods.incomingPurposeProjection(row).finishedOutput,11);
row.incoming_quantity=9;row.surplus_disposition='semi_finished_reserve';
assert.deepEqual(methods.incomingPurposeProjection(row),{orderSheets:8,reserveSheets:1,finishedOutput:8},'same order-purpose quantity may use its authoritative projection');
row.remaining_order_purpose_sheet_qty=0;row.incoming_quantity=3;
assert.deepEqual(methods.incomingPurposeProjection(row),{orderSheets:0,reserveSheets:3,finishedOutput:0});
row.requires_component_processing=true;row.remaining_order_purpose_sheet_qty=8;row.incoming_quantity=8;
row.expected_finished_output_qty=0;row.semi_finished_reserve_expected_finished_output_qty=0;
assert.equal(methods.incomingPurposeProjection(row).finishedOutput,0,'component-processing gate must retain the server zero-output result');
'''
    _run_node(script, tmp_path, 'incoming-quantity-projection.cjs')

"""Expose the existing audited command only for eligible draft details."""
import json
import re

from tests.test_p1_46_common_box_final_layout import INDEX, _run_node


def test_cancel_entry_hides_locked_finance_and_disables_inflight(tmp_path):
    button = re.search(r'<button\b[^>]*data-action="cancel-unissued-statement-draft"[^>]*>', INDEX)
    assert button
    attrs = dict(re.findall(r'([:@\w-]+)="([^"]*)"', button.group()))
    assert attrs['@click'] == 'cancelStatement(statementDetail)'
    _run_node(tmp_path, f"""
const assert=require('node:assert/strict');
const vm={{canFinance:true,financeStatementOperationState:{{action:''}},
 statementDetail:{{confirmation_status:'draft',invoiced_amount:'0',settled_amount:'0',merged_into_statement_id:null}}}};
const visible=new Function('vm',{json.dumps('with(vm){return !!(' + attrs['v-if'] + ');}')});
const disabled=new Function('vm',{json.dumps('with(vm){return (' + attrs[':disabled'] + ');}')});
assert.equal(visible(vm),true);
assert.equal(disabled(vm),false);
vm.canFinance=false;assert.equal(visible(vm),false);vm.canFinance=true;
for(const status of ['confirmed','cancelled','disputed']){{
 vm.statementDetail.confirmation_status=status;assert.equal(visible(vm),false);
}}
vm.statementDetail.confirmation_status='draft';
for(const key of ['invoiced_amount','settled_amount','merged_into_statement_id']){{
 const before=vm.statementDetail[key];vm.statementDetail[key]=1;
 assert.equal(visible(vm),false);vm.statementDetail[key]=before;
}}
vm.financeStatementOperationState.action='cancel';assert.equal(disabled(vm),true);
""")


def test_known_purchase_price_entry_reaches_existing_freeze_before_first_receipt(tmp_path):
    button = re.search(r'<button\b[^>]*data-action="explicit-purchase-receipt-price"[^>]*>', INDEX)
    assert button
    attrs = dict(re.findall(r'([:@\w-]+)="([^"]*)"', button.group()))
    assert attrs['@click'] == 'openPurchaseReceiptFact(row)'
    _run_node(tmp_path, f"""
const assert=require('node:assert/strict');
const perms=new Set(['requisition.purchase_price.confirm']);
const vm={{canViewCosts:true,hasPermission:p=>perms.has(p)}};
const row={{purpose_status:'frozen',receipt_fact_ready:false}};
const visible=new Function('vm','row',{json.dumps('with(vm){return !!(' + attrs['v-if'] + ');}')});
assert.equal(visible(vm,row),true,'known price must be entered before physical receipt');
row.receipt_fact_ready=true;assert.equal(visible(vm,row),false);
perms.add('requisition.purchase_price.correct');assert.equal(visible(vm,row),true);
vm.canViewCosts=false;assert.equal(visible(vm,row),false);vm.canViewCosts=true;
perms.delete('requisition.purchase_price.confirm');assert.equal(visible(vm,row),false);
perms.add('requisition.purchase_price.confirm');row.purpose_status='legacy_unset';
assert.equal(visible(vm,row),false);
""")

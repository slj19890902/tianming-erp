"""Exercise the real scan and price-confirmation methods with Vue reactivity."""
from pathlib import Path

from tests import test_erp_review_receipt_price_form as form_tests


INDEX = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
METHODS = "async loadSupplierReceiptPriceIssues(append = false) {" + INDEX.split(
    "          async loadSupplierReceiptPriceIssues(append = false) {", 1
)[1].split("          async previewSupplierPriceAdoptions() {", 1)[0]


def run_case(monkeypatch, case):
    monkeypatch.setattr(form_tests, "METHODS", METHODS)
    form_tests.run_case("""
ui.canViewFinanceCosts = true;
ui.supplierReceiptPriceIssuesState = {loading:false,loaded:false,error:'',items:[],hasMore:false,nextAfterId:null,actorId:null,authGeneration:null};
const row = id=>({source_key:`paperboard:${id}`,code:'PAPERBOARD_FROZEN_PRICE_MISSING',can_confirm:true});
let gets = [], nextResponse = {issues:[row(17)],has_more:false,next_after_id:null};
const contextGet = axios.get;
axios.get = async(url,options)=>{gets.push({url,options}); return url.endsWith('/receipt-price-issues') ? {data:nextResponse} : contextGet(url);};
""" + case)


def test_active_query_is_get_only_and_pagination_keeps_unique_rows(monkeypatch):
    run_case(monkeypatch, """
nextResponse = {issues:[row(17)],has_more:true,next_after_id:17};
assert.equal(await ui.loadSupplierReceiptPriceIssues(),true);
assert.equal(gets.length,1);
assert.deepEqual(gets[0].options.params,{after_id:0,limit:100});
assert.equal(ui.supplierReceiptPriceIssuesState.items.length,1);
nextResponse = {issues:[row(17),row(19)],has_more:false,next_after_id:null};
assert.equal(await ui.loadSupplierReceiptPriceIssues(true),true);
assert.deepEqual(gets[1].options.params,{after_id:17,limit:100});
assert.deepEqual(Array.from(ui.supplierReceiptPriceIssuesState.items,x=>x.source_key),['paperboard:17','paperboard:19']);
assert.equal(await ui.loadSupplierReceiptPriceIssues(true),false);
assert.equal(gets.length,2);
assert.equal(posts.length,0);
""")


def test_duplicate_click_and_late_session_result_do_not_replace_current_results(monkeypatch):
    run_case(monkeypatch, """
let release;
axios.get=async()=>new Promise(resolve=>{release=resolve;});
const pending=ui.loadSupplierReceiptPriceIssues();
assert.equal(await ui.loadSupplierReceiptPriceIssues(),false);
const finishOld=release;
ui.authGeneration++;
const newPending=ui.loadSupplierReceiptPriceIssues();
release({data:{issues:[row(19)],has_more:false,next_after_id:null}});
assert.equal(await newPending,true);
finishOld({data:{issues:[row(17)],has_more:false,next_after_id:null}});
assert.equal(await pending,false);
assert.deepEqual(Array.from(ui.supplierReceiptPriceIssuesState.items,x=>x.source_key),['paperboard:19']);
assert.equal(ui.supplierReceiptPriceIssuesState.loading,false);
assert.equal(posts.length,0);
""")


def test_failure_keeps_rows_and_permissions_or_blocked_rows_never_start_work(monkeypatch):
    run_case(monkeypatch, """
await ui.loadSupplierReceiptPriceIssues();
axios.get=async()=>{throw Error('read failed');};
assert.equal(await ui.loadSupplierReceiptPriceIssues(),false);
assert.equal(ui.supplierReceiptPriceIssuesState.items.length,1);
assert.equal(ui.supplierReceiptPriceIssuesState.error,'read failed');
assert.equal(ui.supplierReceiptPriceIssuesState.loading,false);
ui.canViewFinanceCosts=false;
assert.equal(await ui.loadSupplierReceiptPriceIssues(),false);
assert.equal(await ui.openSupplierReceiptPrice({...row(17),can_confirm:false}),false);
assert.equal(posts.length,0);
""")


def test_old_session_scan_does_not_block_current_price_context(monkeypatch):
    run_case(monkeypatch, """
ui.supplierReceiptPriceIssuesState={...ui.supplierReceiptPriceIssuesState,actorId:1,authGeneration:0,loading:true};
assert.equal(await ui.openSupplierReceiptPrice(row(17)),true);
assert.equal(ui.modal.type,'supplierReceiptPrice');
assert.equal(posts.length,0);
""")


def test_confirm_removes_only_committed_issue_even_if_monthly_refresh_fails(monkeypatch):
    run_case(monkeypatch, """
nextResponse={issues:[row(17),row(19)],has_more:true,next_after_id:19};
await ui.loadSupplierReceiptPriceIssues();
await ui.openSupplierReceiptPrice(row(17)); fill();
ui.loadSupplierSettlements=async()=>false;
assert.equal(await ui.confirmSupplierReceiptPrice(),true);
assert.deepEqual(Array.from(ui.supplierReceiptPriceIssuesState.items,x=>x.source_key),['paperboard:19']);
assert.equal(ui.supplierReceiptPriceIssuesState.hasMore,true);
assert.equal(await ui.confirmSupplierReceiptPrice(),false);
assert.equal(posts.length,2);
assert.match(notices.at(-1).message,/核价已保存.*尚未刷新/);
""")


def test_uncertain_confirmation_keeps_issue_until_same_request_replay_succeeds(monkeypatch):
    run_case(monkeypatch, """
await ui.loadSupplierReceiptPriceIssues();
await ui.openSupplierReceiptPrice(row(17)); fill();
const normalPost=axios.post;
axios.post=async(url,payload)=>{const result=await normalPost(url,payload); if(url.endsWith('/confirm')) throw Error('lost'); return result;};
assert.equal(await ui.confirmSupplierReceiptPrice(),false);
assert.equal(ui.supplierReceiptPriceIssuesState.items.length,1);
const original=posts[1].payload;
axios.post=normalPost;
assert.equal(await ui.confirmSupplierReceiptPrice(),true);
assert.equal(ui.supplierReceiptPriceIssuesState.items.length,0);
assert.deepEqual(posts[2].payload,original);
assert.equal(posts.length,3);
""")

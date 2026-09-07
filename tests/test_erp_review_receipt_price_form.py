"""Run the real receipt-price methods with Vue reactivity and controlled HTTP outcomes."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
METHODS = INDEX.split("          supplierReceiptPriceIsCurrent(form) {", 1)[1].split(
    "          async previewSupplierPriceAdoptions() {", 1
)[0]
METHODS = "supplierReceiptPriceIsCurrent(form) {" + METHODS


def run_case(case: str) -> None:
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const vueContext = {};
vm.runInNewContext(fs.readFileSync(VUE_PATH, 'utf8'), vueContext);
const {reactive} = vueContext.Vue;
let posts = [], notices = [], key = 0;
const source = {incoming_receipt_item_id:17,source_hash:'source-1',received_quantity:10,report_length_mm:1000,report_width_mm:500};
const axios = {get:async()=>({data:{...source}}),post:async(url,payload)=>{posts.push({url,payload});return {data:{plan_hash:'plan-1'}};}};
const methods = new Function('axios','createIdempotencyKey','return ({'+METHOD_SOURCE+'})')(axios,()=>`key-${++key}`);
const ui = reactive({canFinance:true,user:{id:1},authGeneration:1,supplierSettlementState:{action:''},supplierReceiptPriceForm:null,modal:null,
showToast:(message,error)=>notices.push({message,error}),errorMessage:e=>e.message||'error',loadSupplierSettlements:async()=>true,...methods});
const issue = {code:'PAPERBOARD_FROZEN_PRICE_MISSING',source_key:'paperboard:17'};
const fill = ()=>Object.assign(ui.supplierReceiptPriceForm,{evidence_reference:'BILL-TEST / 1',unit_price:'2',document_amount:'10'});
(async()=>{
CASE
})().catch(e=>{console.error(e);process.exit(1);});
""".replace("VUE_PATH", json.dumps(str(ROOT / "static/vendor/vue-3.5.40.global.prod.js")))
    script = script.replace("METHOD_SOURCE", json.dumps(METHODS)).replace("CASE", case)
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_open_reactive_form_and_local_validation_do_not_write():
    run_case("""
assert.equal(await ui.openSupplierReceiptPrice(issue),true);
assert.equal(ui.modal.type,'supplierReceiptPrice');
assert.equal(ui.supplierReceiptPriceForm.price_unit,'per_square_meter');
assert.equal(posts.length,0);
for(const value of ['',null,0,-1,'NaN']) {
 fill(); ui.supplierReceiptPriceForm.unit_price=value;
 assert.equal(await ui.confirmSupplierReceiptPrice(),false);
 assert.equal(posts.length,0);
}
ui.canFinance=false;
assert.equal(await ui.openSupplierReceiptPrice(issue),false);
assert.equal(await ui.confirmSupplierReceiptPrice(),false);
""")


def test_one_click_previews_then_confirms_and_committed_read_failure_cannot_resubmit():
    run_case("""
await ui.openSupplierReceiptPrice(issue); fill();
ui.loadSupplierSettlements=async()=>false;
assert.equal(await ui.confirmSupplierReceiptPrice(),true);
assert.equal(posts.length,2);
assert.ok(posts[0].url.endsWith('/preview'));
assert.ok(posts[1].url.endsWith('/confirm'));
assert.equal(posts[1].payload.expected_source_hash,'source-1');
assert.equal(posts[1].payload.expected_plan_hash,'plan-1');
assert.equal(posts[1].payload.tax_rate,'0.13');
assert.equal(posts[1].payload.evidence_reference,'BILL-TEST / 1');
assert.equal(ui.supplierReceiptPriceForm.committed,true);
assert.match(notices.at(-1).message,/核价已保存.*尚未刷新/);
assert.equal(await ui.confirmSupplierReceiptPrice(),false);
assert.equal(await ui.openSupplierReceiptPrice(issue),false);
assert.equal(posts.length,2);
""")


def test_uncertain_confirmation_retries_frozen_payload_without_new_preview():
    run_case("""
await ui.openSupplierReceiptPrice(issue); fill();
const normalPost=axios.post;
axios.post=async(url,payload)=>{await normalPost(url,payload); if(url.endsWith('/confirm')) throw Error('network lost'); return {data:{plan_hash:'plan-1'}};};
assert.equal(await ui.confirmSupplierReceiptPrice(),false);
const original=posts[1].payload;
assert.ok(Object.isFrozen(original));
assert.equal(await ui.openSupplierReceiptPrice({...issue,source_key:'paperboard:18'}),false);
fill(); ui.supplierReceiptPriceForm.unit_price='999';
axios.post=normalPost;
assert.equal(await ui.confirmSupplierReceiptPrice(),true);
assert.equal(posts.length,3);
assert.equal(posts[2].payload,original);
assert.equal(posts[2].payload.unit_price,'2');
""")


def test_double_click_and_session_change_during_preview_never_confirm():
    run_case("""
await ui.openSupplierReceiptPrice(issue); fill();
let resolve;
axios.post=async(url,payload)=>{posts.push({url,payload});return new Promise(r=>{resolve=r;});};
const first=ui.confirmSupplierReceiptPrice();
assert.equal(await ui.confirmSupplierReceiptPrice(),false);
assert.equal(posts.length,1);
ui.authGeneration=2; ui.user={id:2};
resolve({data:{plan_hash:'plan-1'}});
assert.equal(await first,false);
assert.equal(posts.length,1);
assert.equal(ui.supplierReceiptPriceForm.saving,false);
""")


def test_amount_mismatch_preserves_input_and_cancel_has_no_confirmation():
    run_case("""
await ui.openSupplierReceiptPrice(issue); fill();
axios.post=async(url,payload)=>{posts.push({url,payload});const e=Error('凭据金额不一致');e.response={status:422};throw e;};
assert.equal(await ui.confirmSupplierReceiptPrice(),false);
assert.equal(posts.length,1);
assert.equal(ui.supplierReceiptPriceForm.document_amount,'10');
assert.equal(ui.supplierReceiptPriceForm.confirmPayload,null);
assert.match(ui.supplierReceiptPriceForm.error,/不一致/);
ui.modal=null;
assert.equal(posts.length,1);
""")

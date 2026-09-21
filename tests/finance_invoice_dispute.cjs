const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('static/index.html','utf8');
function method(start,end){return html.split(start)[1].split(end)[0].replace(/},\s*$/, '');}
const errorMessage=new Function('error',method('errorMessage(error) {','normalizeValidationErrors(error, fieldMap = {}) {'));
assert.equal(errorMessage({response:{data:{detail:{message:'客户开票资料不完整',missing_items:['已确认的客户默认项目规则']}}}}),'客户开票资料不完整；缺项：已确认的客户默认项目规则');
assert.equal(errorMessage({response:{data:{detail:'版本冲突'}}}),'版本冲突');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const save=new AsyncFunction(method('async saveStatementDispute() {','async openStatementEdit(row) {'));
let submitted,confirmCount=0;
global.confirm=()=>{confirmCount++;return true;};
global.axios={post:async(url,payload)=>{submitted={url,payload};return {data:{...state.statementDetail,version:3,confirmation_status:'draft'}};}};
const state={statementDisputeState:{saving:false},statementDetail:{id:1,version:2,confirmation_status:'confirmed',items:[{source_type:'delivery',statement_item_id:1,unit_price_snapshot:'10',actual_received_quantity:10}]},statementDisputeForm:{reason:'更正单价数量',remove:{},add:{},edits:{1:{unit_price:'12.50',quantity:8}}},showToast(){},closeModal(){},loadFinance:async()=>{},errorMessage};
(async()=>{assert.equal(await save.call(state),true);assert.equal(confirmCount,1);assert.equal(submitted.payload.update_lines[0].quantity,undefined);assert.equal(submitted.payload.update_lines[0].unit_price,'12.50');assert.equal(submitted.payload.remove_lines.length,0);console.log('PASS missing items and same-month dispute UI submission');})().catch(error=>{console.error(error);process.exitCode=1});

const openReceiptCorrection=new AsyncFunction('item',method('async openDisputeReceipt(item) {','async reopenStatementDispute() {'));
(async()=>{
 let opened;
 const ctx={statementDisputeState:{saving:false},statementDetail:{id:9,version:2,confirmation_status:'confirmed',items:[]},statementDisputeForm:{reason:'实际签收录错',edits:{},remove:{},add:{}},showToast(){},async reopenStatementDispute(){this.statementDetail.version=3;this.statementDetail.confirmation_status='draft';return true;},async openReceipt(row){opened=row;this.receiptForm={};this.modal={};return true;}};
 assert.equal(await openReceiptCorrection.call(ctx,{delivery_id:7,return_receipt_id:8,delivery_number:'D7'}),true);
 assert.equal(opened.id,7);assert.equal(opened.return_receipt_id,8);
 assert.equal(ctx.receiptForm.statement_correction.expected_statement_version,3);
 assert.equal(ctx.receiptForm.statement_correction.statement_id,9);
 console.log('PASS receipt correction routing and fresh statement version');
})().catch(error=>{console.error(error);process.exitCode=1});

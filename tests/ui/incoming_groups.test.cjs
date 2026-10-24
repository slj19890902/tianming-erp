const {test}=require('node:test');
const assert=require('node:assert/strict');
const {groups,distribute,methods}=require('../../static/js/incoming-groups.js');
test('group only server-authorized frozen identities; expand is initially closed',()=>{
 const rows=[{item_id:'r1',receipt_group_key:'a'},{item_id:'r2',receipt_group_key:'a'},{item_id:'r3',receipt_group_key:'b'}];
 assert.deepEqual(groups(rows).map(g=>g.rows.length),[2,1]);
 const ctx={incomingExpandedGroups:{}};
 assert.equal(ctx.incomingExpandedGroups.a,undefined);
 methods.toggleIncomingGroup.call(ctx,{key:'a'});assert.equal(ctx.incomingExpandedGroups.a,true);
});
test('total actual sheets apportion exactly, including partial receipts and integer remainder',()=>{
 const rows=[{remaining_quantity:300},{remaining_quantity:150}];
 assert.deepEqual(distribute(rows,450),[300,150]);
 assert.deepEqual(distribute(rows,400),[267,133]);
 assert.deepEqual(distribute(rows,1),[1,0]);
 assert.deepEqual(distribute(rows,600),[400,200]);
 assert.throws(()=>distribute(rows,1.5));
});
test('group submit includes all positive rows in one atomic request; zero-share rows remain pending',async()=>{
 const first={item_id:'r1',incoming_quantity:1},second={item_id:'r2',incoming_quantity:0};
 let sent;await methods.receiveIncomingGroup.call({batchReceiveIncoming:async rows=>sent=rows},{rows:[first,second]});
 assert.deepEqual(sent,[first]);
});

const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'../../static/index.html'),'utf8');
const batchText=html.slice(html.indexOf('          async batchReceiveIncoming(groupRows=null) {'),html.indexOf('          async receiveIncoming(row) {'));
function batchContext(put) {
 const fn=vm.runInNewContext('({'+batchText+'})',{axios:{put},createIdempotencyKey:(()=>{let n=0;return()=>`group-test-${++n}`;})(),confirm:()=>true}).batchReceiveIncoming;
 const rows=[{item_id:'r1',incoming_quantity:125},{item_id:'r2',incoming_quantity:134}];
 const ctx={incomingPending:rows,incomingSelected:{r1:true,r2:true},incomingBatchReceiveAttempt:null,incomingPendingAppliedPage:1,
  user:{id:1},authGeneration:1,incomingPriceRecovery:{},messages:[],canReceiveIncoming:()=>true,
  ensureAutomaticPurchaseReceiptFact:async()=>{},incomingPayload:r=>({item_id:r.item_id,received_quantity:r.incoming_quantity}),
  errorMessage:e=>e.message,showToast(m){this.messages.push(m);},refreshIncomingAfterWrite:async()=>true,showIncomingNextStepGuide(){}};
 ctx.batchReceiveIncoming=fn.bind(ctx);return ctx;
}
test('actual desktop method submits the group atomically and preserves original request after uncertain outcome',async()=>{
 const sent=[];
 const ctx=batchContext(async(url,body)=>{sent.push(JSON.parse(JSON.stringify(body)));if(sent.length===1)throw Error('network');return {data:{succeeded:2,results:[]}};});
 await ctx.batchReceiveIncoming(ctx.incomingPending);
 assert.equal(sent[0].atomic,true);
 ctx.incomingPending[0].incoming_quantity=999;
 await ctx.batchReceiveIncoming();
 assert.deepEqual(sent[1],sent[0]);
 assert.equal(ctx.incomingBatchReceiveAttempt,null);
});
test('one unready child prevents the whole group submission',async()=>{
 let sent=0;const ctx=batchContext(async()=>{sent++;});
 ctx.ensureAutomaticPurchaseReceiptFact=async row=>{if(row.item_id==='r2')throw Error('missing frozen price');};
 await ctx.batchReceiveIncoming(ctx.incomingPending);
 assert.equal(sent,0);assert.equal(ctx.incomingBatchReceiveAttempt,null);
 assert.match(ctx.messages[0],/成功 0 条/);
});
test('BOM purchase draft totals retain each product and different crease cannot merge',()=>{
 const a={customer_id:1,material_display:'K8K',cardboard_len:700,cardboard_width:950,purchase_total_sheet_qty:125,crease_type:'无压线'};
 const b={...a,purchase_total_sheet_qty:134};const c={...a,purchase_total_sheet_qty:250};
 const ctx={...methods,onPurchasePurposeTotalChanged:r=>{r.order_purpose_sheet_qty=r.purchase_total_sheet_qty;},showToast:assert.fail};
 const result=ctx.bomDraftGroups({bom_lines:[a,b,c,{...a,crease_type:'有压线'}]});
 assert.deepEqual(result.map(g=>g.rows.length),[3,1]);
 assert.equal(ctx.incomingGroupQuantity(result[0],'purchase_total_sheet_qty'),509);
 assert.equal(result[0].first._physicalExpanded,undefined);
 ctx.setBomDraftTotal(result[0],510);
 assert.equal(a.purchase_total_sheet_qty+b.purchase_total_sheet_qty+c.purchase_total_sheet_qty,510);
});

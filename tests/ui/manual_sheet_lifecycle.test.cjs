const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
const root=require('node:path').resolve(__dirname,'../..');
const html=fs.readFileSync(root+'/static/index.html','utf8');
function method(name){
 const match=new RegExp('^ {10}(?:async )?'+name+'\\([^\\n]*\\) \\{','m').exec(html);
 assert.ok(match,name);
 const tail=html.slice(match.index+match[0].length),end=/\n {10}(?:async )?[\w$]+\([^\n]*\) \{/.exec(tail);
 assert.ok(end,name+' end');
 return html.slice(match.index,match.index+match[0].length+end.index).trim().replace(/,$/,'');
}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
const preview=()=>({expected_order_item_revision:'b'.repeat(64),remaining_required_piece_qty:20,pieces_per_box:1,items:[{
 lot_id:7,version:1,selectable:true,max_stock_quantity:20,available_stock_quantity:20,stock_yield_per_sheet:1,
 board_length_mm:200,board_width_mm:300,material_code:'TEST',flute_type:'B',review_hash:'a'.repeat(64),warning_codes:[]}]});
async function harness({post,get,removeAfterSuccess=true}={}){
 const {window}=parseHTML('<html><head></head><body><div id="app"></div></body></html>');
 window.HTMLElement.prototype.focus=function(){};
 const context={console,window,document:window.document,Document:window.Document,ShadowRoot:window.ShadowRoot,Element:window.Element,HTMLElement:window.HTMLElement,SVGElement:window.SVGElement,setTimeout,clearTimeout};
 vm.createContext(context);
 vm.runInContext(fs.readFileSync(root+'/static/vendor/vue-3.5.40.global.prod.js','utf8'),context);
 vm.runInContext(fs.readFileSync(root+'/static/ui/manual-sheet-deduction.js','utf8'),context);
 let reads=0,writes=0;const requests=[];
 window.axios=context.axios={get:async(...args)=>{reads++;return get?get(reads,...args):{data:preview()}},post:async(url,payload)=>{
  writes++;requests.push({url,payload:JSON.parse(JSON.stringify(payload))});return post?post(writes,url,payload):{data:{message:'成功',remaining_required_piece_qty:0}};
 }};
 context.TMManualSheetDeduction=window.TMManualSheetDeduction;
 const methods=vm.runInContext('({'+['manualSheetParentControlsBlocked','manualSheetPendingOperations','requisitionInventoryStatusMessage','requisitionInventoryCanReconcile','requisitionInventoryBaseActionBlocked','manualSheetDeductionDisabled','setManualSheetOperation','requisitionInventoryActionBlocked','requisitionInventoryActionLabel','resetRequisitionInventoryActionState','refreshRequisitionInventoryState','refreshManualBomSheetDeduction'].map(method).join(',')+'})',context);
 const app=context.Vue.createApp({components:{sheet:window.TMManualSheetDeduction.component},
  data(){return {user:{id:1},authGeneration:1,source:{order_item_id:1},showRow:true,manualSheetOperations:{},modal:null,requisitionForm:{items:[]},supplierRequisitionDraft:{supplier_groups:[]},toasts:[],
   requisitionInventoryActionState:{action:'',committed:false,outcomeUncertain:false,context:'',orderItemId:null,bomSnapshotId:null,targetKey:'',result:null}};},
  methods:{...methods,showToast(...a){this.toasts.push(a)},errorMessage(e){return e.message},async loadRequisition(){if(removeAfterSuccess)this.showRow=false;return true;}},
  template:'<div><button id="parent-action" :disabled="requisitionInventoryActionBlocked()">报料</button><sheet v-if="showRow" ref="sheet" :actor-id="1" :order-item-id="1" :disabled="manualSheetDeductionDisabled()" :operation-reporter="setManualSheetOperation" @operation-state="setManualSheetOperation($event)" @reserved="refreshManualBomSheetDeduction(source,$event)"/></div>'});
 const parent=app.mount(window.document.querySelector('#app'));
 const tick=async()=>{for(let i=0;i<5;i++){await Promise.resolve();await context.Vue.nextTick();}};
 await tick();const sheet=parent.$refs.sheet;await sheet.open();await tick();sheet.rows[0].selected=true;sheet.rows[0].inputQuantity=20;
 return {parent,sheet,tick,app,window,ui:window.TMManualSheetDeduction,requests,get writes(){return writes}};
}
test('real Vue: successful reserve removing the source row cannot strand the parent lock',async()=>{
 const fresh=deferred(),h=await harness({get:(n)=>n===1?{data:preview()}:fresh.promise});
 try{
  const work=h.sheet.confirmSelection();await h.tick();assert.equal(h.parent.showRow,false);assert.equal(h.parent.$refs.sheet,null);
  assert.equal(h.parent.manualSheetParentControlsBlocked(),false,'confirmed write waited for an unrelated preview read');
  fresh.resolve({data:preview()});await work;await h.tick();
  assert.equal(h.ui.pending.size,0);assert.equal(h.writes,1);
  assert.equal(h.parent.manualSheetParentControlsBlocked(),false,'successful detached component left a parent lock');
  assert.equal(h.parent.requisitionInventoryActionBlocked(),false);
 }finally{h.app.unmount();}
});
test('parent reconciliation settles the original unknown sheet request with the same immutable payload',async()=>{
 const receipts=new Map();let deductions=0;
 const h=await harness({post:(n,url,payload)=>{
  if(!receipts.has(payload.idempotency_key)){deductions++;receipts.set(payload.idempotency_key,{message:'原回执',remaining_required_piece_qty:0});}
  if(n===1)throw Error('lost response');return {data:receipts.get(payload.idempotency_key)};
 }});
 try{
  await h.sheet.confirmSelection();await h.tick();assert.equal(h.ui.pending.size,1);assert.equal(h.parent.manualSheetParentControlsBlocked(),true);
  const original=h.requests[0];await h.parent.refreshRequisitionInventoryState();await h.tick();
  assert.equal(h.writes,2,'reconcile did not settle the sheet request');assert.deepEqual(h.requests[1],original);
  assert.equal(deductions,1);
  assert.equal(h.ui.pending.size,0);assert.equal(h.parent.requisitionInventoryActionBlocked(),false);
 }finally{h.app.unmount();}
});
test('submitting is not presented as an unknown outcome and parent reconcile cannot duplicate it',async()=>{
 const response=deferred(),h=await harness({post:()=>response.promise,removeAfterSuccess:false});
 try{
  const work=h.sheet.confirmSelection();await h.tick();
  assert.match(h.parent.requisitionInventoryStatusMessage(),/正在使用库存片料/);
  assert.equal(h.parent.requisitionInventoryCanReconcile(),false);
  assert.equal(await h.parent.refreshRequisitionInventoryState(),false);assert.equal(h.writes,1);
  response.resolve({data:{message:'成功',remaining_required_piece_qty:0}});await work;await h.tick();
  assert.equal(h.parent.requisitionInventoryActionBlocked(),false);
 }finally{h.app.unmount();}
});
test('a lost response after real Vue unmount can still be reconciled from the owner',async()=>{
 const response=deferred(),h=await harness({post:n=>n===1?response.promise:{data:{message:'原回执',remaining_required_piece_qty:0}}});
 try{
  const work=h.sheet.confirmSelection();await h.tick();h.parent.showRow=false;await h.tick();
  response.reject(Error('connection lost'));await work;await h.tick();
  assert.equal(h.parent.manualSheetParentControlsBlocked(),true);assert.equal(h.parent.requisitionInventoryCanReconcile(),true);
  await h.parent.refreshRequisitionInventoryState();await h.tick();
  assert.equal(h.writes,2);assert.deepEqual(h.requests[0],h.requests[1]);assert.equal(h.parent.requisitionInventoryActionBlocked(),false);
 }finally{h.app.unmount();}
});
test('reconciliation that is still offline keeps the exact request and lock',async()=>{
 const h=await harness({post:()=>{throw Error('offline');}});
 try{
  await h.sheet.confirmSelection();await h.tick();
  assert.equal(await h.parent.refreshRequisitionInventoryState(),false);await h.tick();
  assert.equal(h.ui.pending.size,1);assert.deepEqual(h.requests[0],h.requests[1]);
  assert.equal(h.parent.manualSheetParentControlsBlocked(),true);assert.equal(h.parent.requisitionInventoryActionState.action,'');
  assert.match(h.parent.toasts.at(-1)[0],/仍未确认/);
 }finally{h.app.unmount();}
});
test('known successful write stays unlocked when its follow-up preview fails',async()=>{
 const h=await harness({get:n=>{if(n>1)throw Error('read failed');return {data:preview()};}});
 try{
  await h.sheet.confirmSelection();await h.tick();assert.equal(h.writes,1);assert.equal(h.ui.pending.size,0);
  assert.equal(h.parent.requisitionInventoryActionBlocked(),false);
 }finally{h.app.unmount();}
});
test('an explicit rejection on reconciliation is reported as rejected, not a successful deduction',async()=>{
 const h=await harness({post:n=>{const error=Error(n===1?'lost response':'库存已变');if(n>1)error.response={status:409};throw error;}});
 try{
  await h.sheet.confirmSelection();await h.parent.refreshRequisitionInventoryState();await h.tick();
  assert.equal(h.ui.pending.size,0);assert.equal(h.parent.requisitionInventoryActionBlocked(),false);
  assert.match(h.parent.toasts.at(-1)[0],/原片料请求未完成.*库存已变/);
 }finally{h.app.unmount();}
});
test('late release removes only its original actor lock without unlocking another actor',async()=>{
 const h=await harness();
 try{
  h.parent.setManualSheetOperation({identity:'1:order:1:whole',blocked:true});h.parent.user={id:2};
  h.parent.setManualSheetOperation({identity:'2:order:2:whole',blocked:true});
  h.parent.setManualSheetOperation({identity:'1:order:1:whole',blocked:false});
  assert.deepEqual(Object.keys(h.parent.manualSheetOperations),['2:order:2:whole']);
  assert.equal(h.parent.manualSheetParentControlsBlocked(),true);
 }finally{h.app.unmount();}
});

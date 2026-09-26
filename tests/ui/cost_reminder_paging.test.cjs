const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=path.resolve(__dirname,'../..'),html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const section=html.slice(html.indexOf('          newCostPanelState() {'),html.indexOf('          customerChargeTypeText(value)'));
const methods=Function(`return ({${section}})`)();
const context=vm.createContext({console});vm.runInContext(fs.readFileSync(path.join(root,'static/vendor/vue-3.5.40.global.prod.js'),'utf8'),context);
function makeVM(){
 const state=context.Vue.reactive({authGeneration:1,canViewCosts:true,costPanelTab:'gaps',costGapState:methods.newCostPanelState(),costReviewState:methods.newCostPanelState(),errorMessage:e=>e.message,...methods});
 Object.defineProperty(state,'costPanelState',{get(){return state.costPanelTab==='gaps'?state.costGapState:state.costReviewState;}});
 return state;
}
test('pagination requests exact server filters and rejects stale responses and identity changes',async()=>{
 const state=makeVM(),pending=[];const request=options=>new Promise((resolve,reject)=>pending.push({options,resolve,reject}));
 state.costGapState.keyword=' 客户甲 ';state.costGapState.filter='supplier_price';
 const old=state.loadCostPanelPage('gaps',2,request);
 state.costGapState.keyword='客户乙';const latest=state.loadCostPanelPage('gaps',1,request);
 assert.deepEqual(pending[0].options.params,{page:2,page_size:20,keyword:'客户甲',category:'supplier_price'});
 pending[1].resolve({data:{items:[{item_id:2}],total_items:1,page:1,page_size:20,page_count:1}});assert.equal(await latest,true);
 pending[0].resolve({data:{items:[{item_id:1}],total_items:99}});assert.equal(await old,false);
 assert.equal(state.costGapState.items[0].item_id,2);assert.equal(state.costGapState.applied_keyword,'客户乙');
 const switched=state.loadCostPanelPage('gaps',1,request);state.authGeneration++;
 pending[2].resolve({data:{items:[{item_id:9}]}});assert.equal(await switched,false);assert.equal(state.costGapState.items.length,0);
 const retry=state.loadCostPanelPage('review',3,request);pending[3].reject(Error('offline'));assert.equal(await retry,false);assert.equal(state.costReviewState.error,'offline');assert.equal(state.costReviewState.loading,false);
 const closed=state.loadCostPanelPage('gaps',1,request);state.costGapState.requestId++;pending[4].resolve({data:{items:[{item_id:5}]}});assert.equal(await closed,false);
});
test('cost row opens exact item and restores original page, category and scroll on return',async()=>{
 const state=makeVM();let focused,scrolled,reloaded;const mask={scrollTop:317};
 global.document={querySelector:()=>mask,getElementById:id=>({focus(){focused=id;},scrollIntoView(){scrolled=id;}})};
 Object.assign(state,{$refs:{},$nextTick:async()=>{},cancelOrderReadDetailRequests(){},async openOrderDetail(row){this.orderDetail={id:row.id};this.modal={type:'orderDetail'};return true;},async loadActiveCostPanel(page){reloaded=page;}});
 state.costGapState.page=4;state.costGapState.filter='supplier_price';state.costGapState.keyword='甲';
 assert.equal(await state.openCostGapOrder({order_id:12,item_id:37}),true);
 assert.equal(scrolled,'cost-review-item-37');assert.equal(focused,'cost-review-item-37');
 mask.scrollTop=0;assert.equal(await state.returnToCostPanel(),true);
 assert.equal(reloaded,4);assert.equal(state.costGapState.filter,'supplier_price');assert.equal(state.costGapState.keyword,'甲');assert.equal(mask.scrollTop,317);assert.equal(focused,'cost-reminder-37');assert.equal(state.modal.type,'costGaps');
 delete global.document;
});
test('header and focused row templates keep missing PO distinct and profit zero visible',()=>{
 assert.match(html,/customer_po \|\| '未填写客户单号'/);
 assert.match(html,/row\.estimated_gross_profit != null/);
 assert.match(html,/:id="'cost-review-item-'\+item.id"/);
 assert.match(html,/:open="Number\(item.id\)===costReviewFocusItemId"/);
 assert.match(html,/当前 .*共 .*total_items/);
});

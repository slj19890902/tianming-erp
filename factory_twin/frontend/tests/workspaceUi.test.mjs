import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const html=fs.readFileSync(new URL('../../../static/index.html',import.meta.url),'utf8');
const workspace=fs.readFileSync(new URL('../../../static/ui/workspace.js',import.meta.url),'utf8');
test('visible capacity respects viewport, larger rows and group overhead',()=>{
 const sandbox={};vm.runInNewContext(workspace,sandbox);
 const capacity=sandbox.ERPWorkspace.capacity;
 assert.equal(capacity({height:768,top:280,rowHeight:60}),7);
 assert.equal(capacity({height:1080,top:280,rowHeight:60}),12);
 assert.equal(capacity({height:768,top:280,rowHeight:95}),4);
 assert.equal(capacity({height:250,top:280,rowHeight:95}),1);
});
test('history clears old rows immediately; a late cancelled response cannot overwrite a later page',async()=>{
 const start=html.indexOf('          async loadIncomingHistory()');
 const method=html.slice(start,html.indexOf('          resetIncomingHistoryFilters()',start)).trim().replace(/,$/,'');
 const requests=[],controllers=new Map();
 const ctx=vm.runInNewContext(`({${method}})`,{latestRequestControllers:controllers,axios:{get:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))}});
 Object.assign(ctx,{incomingHistory:[{id:'old'}],pages:{incomingHistory:2},incomingListPageSize:()=>7,incomingHistoryFilters:{},
  beginLatestRequest(key){controllers.get(key)?.abort();const c=new AbortController();controllers.set(key,c);return c;},
  finishLatestRequest(key,c){if(controllers.get(key)===c)controllers.delete(key);},isCancelledRequest:()=>false,errorMessage:e=>e.message});
 const second=ctx.loadIncomingHistory();assert.equal(ctx.incomingHistory.length,0);assert.equal(ctx.incomingHistoryLoading,true);
 ctx.pages.incomingHistory=3;const third=ctx.loadIncomingHistory();
 requests[1].resolve({data:{items:[{id:'third'}],total:40}});await third;
 requests[0].resolve({data:{items:[{id:'second'}],total:40}});await second;
 assert.equal(ctx.incomingHistory[0].id,'third');assert.equal(ctx.incomingHistoryLoading,false);
 ctx.pages.incomingHistory=4;const fourth=ctx.loadIncomingHistory();requests[2].reject(new Error('offline'));await fourth;
 assert.equal(ctx.incomingHistory.length,0);assert.match(ctx.incomingHistoryError,/offline/);assert.equal(ctx.incomingHistoryLoading,false);
});

test('all workspace modules parse; component templates compile',()=>{
 const sandbox={console};vm.createContext(sandbox);vm.runInContext(fs.readFileSync(new URL('../../../static/vendor/vue-3.5.40.global.prod.js',import.meta.url),'utf8'),sandbox);
 const definitions=[];const app={mixin(){},component(name,definition){definitions.push([name,definition]);}};
 for(const name of ['workspace','pdf-workspace','purchase-workspace','production-workspace','finance-workspace']){
  vm.runInContext(fs.readFileSync(new URL(`../../../static/ui/${name}.js`,import.meta.url),'utf8'),sandbox);
 }
 for(const key of Object.keys(sandbox).filter(key=>key.startsWith('ERP')))sandbox[key].install(app);
 for(const [name,def] of definitions)if(def.template)sandbox.Vue.compile(def.template,{decodeEntities:s=>s,onError:e=>{throw new Error(`${name}: ${e.message}`);}});
});

test('PDF pagination retains drafts and uses shared inventory candidate ordering',()=>{
 const sandbox={};vm.runInNewContext(fs.readFileSync(new URL('../../../static/ui/pdf-workspace.js',import.meta.url),'utf8'),sandbox);
 let definition;sandbox.ERPPdfWorkspace.install({mixin(d){definition=d;}});
 const ctx={...definition.methods,pdfFitCapacity:2,inventoryCandidateNeedsManualConfirmation:c=>!!c.needs_check};
 const candidateStart=html.indexOf('          rankSemiStockCandidates(');
 const candidateMethods=html.slice(candidateStart,html.indexOf('          semiStockMoreCount(',candidateStart)).trim().replace(/,$/,'');
 Object.assign(ctx,vm.runInNewContext(`({${candidateMethods}})`));
 const items=Array.from({length:35},(_,i)=>({id:i+1})),draft={items,_product_page:2};
 assert.equal(ctx.pdfPageItems(draft)[0],items[30]);assert.equal(items.length,35);
 ctx.pdfFitCapacity=1;
 assert.equal(ctx.pdfPageItems(draft)[0],items[30]);
 ctx.pdfPageItems(draft)[0].quantity=17;
 assert.equal(items[30].quantity,17);
 assert.equal(sandbox.ERPPdfWorkspace.amount({quantity:5,unit_price:''}),null);
 const exact={lot_id:1,available_stock_quantity:10,sheet_type:'net_sheet',signature_differences:[],warning_codes:[]},partial={lot_id:2,available_stock_quantity:10,dimension_distance:1,sheet_type:'net_sheet',signature_differences:['尺寸']},raw={lot_id:3,available_stock_quantity:10,sheet_type:'raw_board'};
 const item={_inventory:{semi:{whole:{candidates:[partial,exact,raw],manual_candidates:[exact]}}}};
 assert.deepEqual([...ctx.pdfMaterialCandidates(item,'whole','semi')].map(r=>r.lot_id),[1,2]);
 assert.deepEqual([...ctx.pdfMaterialCandidates(item,'whole','raw')].map(r=>r.lot_id),[3]);
});

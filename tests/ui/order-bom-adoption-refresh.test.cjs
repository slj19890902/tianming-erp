'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('node:assert/strict');
const {test}=require('node:test');
const html=fs.readFileSync(path.resolve(__dirname,'../../static/index.html'),'utf8');
function method(name){
 const start=new RegExp('^[ \\t]*(?:async )?'+name+'\\([^\\n]*\\) \\{','m').exec(html);assert.ok(start,name);
 const end=/^          \},?$/m.exec(html.slice(start.index));assert.ok(end,name);
 return html.slice(start.index,start.index+end.index+end[0].length).trim().replace(/,$/,'');
}
function setup(){
 const calls=[],toasts=[],controllers=new Map();let handler=async(method,url)=>({data:method==='post'?{message:'adopted'}:url==='/api/orders'?{items:[{id:10,items:[{id:21,covered:500}]}],total:1}:{rows:[{snapshot_id:31,make_units:0}]}});
 const axios={post:async(url,payload)=>{calls.push({method:'post',url,payload});return handler('post',url,payload);},get:async(url)=>{calls.push({method:'get',url});return handler('get',url);}};
 const context=vm.createContext({axios,latestRequestControllers:controllers,createIdempotencyKey:()=> 'adoption-fixed-key',Number});
 const names=['orderBomReviewIsCurrent','useOrderBomFinished','submitOrderBomStockReview','loadOrderBomStockReview','invalidatePageCache','loadOrders'];
 const methods=vm.runInContext('({'+names.map(method).join(',\n')+'})',context);
 const review={itemId:21,actorId:1,authGeneration:1,rows:[{snapshot_id:31,make_units:500}],sequence:0,loading:false,busy:false,pending:null,error:''};
 const state={...methods,user:{id:1},authGeneration:1,canWarehouseReserve:true,canAdmin:false,modal:{type:'orderBomStockReview'},orderBomStockReview:review,
  activePage:'orders',orderWorkspace:'queue',pages:{orders:1},pageSize:20,filters:{},orderListPageSize:()=>20,
  orders:[{id:10,items:[{id:21,covered:0}]}],pageCacheUpdatedAt:{orders:123},orderListGeneration:0,
  beginLatestRequest:key=>{const controller={signal:{}};controllers.set(key,controller);return controller;},
  finishLatestRequest:(key,controller)=>{if(controllers.get(key)===controller)controllers.delete(key);},
  cancelOrderGroupDetailRequests:()=>{},isCancelledRequest:()=>false,showToast:text=>toasts.push(text),errorMessage:e=>e.message};
 return {state,review,calls,toasts,handler:fn=>handler=fn};
}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
test('confirmed BOM adoption invalidates order cache and reads fresh modal and order DTO once',async()=>{
 const h=setup();assert.equal(await h.state.useOrderBomFinished({snapshot_id:31}),true);
 assert.equal(h.calls.length,3);assert.deepEqual(h.calls.map(c=>c.method),['post','get','get']);
 assert.equal(h.calls[0].url,'/api/warehouse/finished/bom-components/31/auto-cover');
 assert.equal(h.calls[2].url,'/api/orders');assert.equal(h.state.orders[0].items[0].covered,500);
 assert.equal(h.state.pageCacheUpdatedAt.orders,undefined);assert.equal(h.review.pending,null);assert.equal(h.review.busy,false);
});
test('double click remains blocked while the one adoption or its refresh is pending',async()=>{
 const h=setup(),wait=deferred();h.handler((method,url)=>method==='post'?wait.promise:Promise.resolve({data:url==='/api/orders'?{items:[],total:0}:{rows:[]}}));
 const first=h.state.useOrderBomFinished({snapshot_id:31});assert.equal(await h.state.useOrderBomFinished({snapshot_id:31}),false);
 assert.equal(h.calls.filter(c=>c.method==='post').length,1);wait.resolve({data:{message:'saved'}});assert.equal(await first,true);
 assert.equal(h.calls.filter(c=>c.method==='post').length,1);
});
test('unknown mutation retains original key and does not refresh or issue another adoption',async()=>{
 const h=setup();h.handler(async()=>{throw Error('lost response');});assert.equal(await h.state.useOrderBomFinished({snapshot_id:31}),false);
 const pending=h.review.pending;assert.ok(pending);assert.equal(pending.payload.idempotency_key,'adoption-fixed-key');
 assert.equal(await h.state.useOrderBomFinished({snapshot_id:31}),false);assert.equal(h.calls.length,1);assert.equal(h.review.pending,pending);
 assert.equal(h.state.pageCacheUpdatedAt.orders,123);
});
test('known successful adoption with failed order GET stays confirmed and never retries POST',async()=>{
 const h=setup();h.handler(async(method,url)=>{if(url==='/api/orders')throw Error('read failure');return {data:method==='post'?{}:{rows:[]}};});
 assert.equal(await h.state.useOrderBomFinished({snapshot_id:31}),true);assert.equal(h.review.pending,null);
 assert.match(h.review.error,/操作已完成.*勿重复采用/);assert.equal(h.calls.filter(c=>c.method==='post').length,1);
 assert.equal(h.state.pageCacheUpdatedAt.orders,undefined);assert.equal(h.state.orders[0].items[0].covered,0);
});
test('explicit replay confirms the original unknown command then refreshes with the same key',async()=>{
 const h=setup();h.handler(async()=>{throw Error('lost response');});await h.state.useOrderBomFinished({snapshot_id:31});
 h.handler(async(method,url)=>({data:method==='post'?{}:url==='/api/orders'?{items:[],total:0}:{rows:[]}}));
 assert.equal(await h.state.submitOrderBomStockReview(),true);
 assert.equal(h.calls[0].payload,h.calls[1].payload);assert.equal(h.review.pending,null);
 assert.equal(h.calls.filter(c=>c.url==='/api/orders').length,1);
});
test('late success for a previous account never refreshes or clears its retained request',async()=>{
 const h=setup(),wait=deferred();h.handler(()=>wait.promise);const first=h.state.useOrderBomFinished({snapshot_id:31});
 h.state.user={id:2};h.state.authGeneration=2;wait.resolve({data:{message:'saved'}});
 assert.equal(await first,false);assert.equal(h.calls.length,1);assert.ok(h.review.pending);
 assert.equal(h.state.pageCacheUpdatedAt.orders,123);assert.equal(h.toasts.length,0);
});
test('permissions and existing pending requests still reject before any mutation',async()=>{
 const h=setup();h.state.canWarehouseReserve=false;assert.equal(await h.state.useOrderBomFinished({snapshot_id:31}),false);
 h.state.canWarehouseReserve=true;h.review.pending={url:'/original',payload:{idempotency_key:'old-key'}};
 assert.equal(await h.state.useOrderBomFinished({snapshot_id:31}),false);assert.equal(h.calls.length,0);
});

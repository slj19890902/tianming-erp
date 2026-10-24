const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {parseHTML}=require('linkedom');
const repo=path.resolve(__dirname,'../..'),code=fs.readFileSync(repo+'/static/ui/replenishment-production.js','utf8'),html=fs.readFileSync(repo+'/static/index.html','utf8');
const endpoint='/api/production/stock-replenishment/41/production-sources';
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
const detail=()=>({stock_replenishment_item_id:41,product_id:3366,product_code:'80011641',product_name:'D4/U-08B',source_fingerprint:'a'.repeat(64),required_output:20,covered_output:0,remaining_output:20,remaining_sheet_quantity:20,can_purchase:true,blocked_reason:'',storage:{location_id:7,location:{location_name:'一楼成品',layout_version:5}},variants:[],jobs:[],scrap_jobs:[],candidates:[{inventory_lot_id:9,version:3,available_quantity:30,board_length_mm:720,board_width_mm:480,material_code:'A',flute_type:'E',location:'一楼片料',yield_per_sheet:1,pieces_per_box:1,recommendation_label:'同尺寸',warning_messages:[]}]});
function storage(){const values=new Map();return {get length(){return values.size},key:i=>[...values.keys()][i],getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,String(v)),removeItem:k=>values.delete(k)};}
async function harness({get,post,store=storage(),jobs=false,canExecute=true}={}){
 const {window}=parseHTML('<html><body><div id="app"></div></body></html>');window.HTMLElement.prototype.focus=function(){};window.sessionStorage=store;window.confirm=()=>true;
 const requests=[];window.axios={get:async(url,config)=>get?get(url,config):{data:url.endsWith('/locations')?{items:[{id:7,layout_version:5}]}:url.endsWith('/jobs')?{items:[]}:detail()},post:async(url,payload)=>{requests.push({url,payload:JSON.parse(JSON.stringify(payload))});return post?post(url,payload,requests.length):{data:url.endsWith('/preview')?{preview_hash:'b'.repeat(64)}:{message:'已保存'}};}};
 const context={console,window,document:window.document,Document:window.Document,Element:window.Element,HTMLElement:window.HTMLElement,SVGElement:window.SVGElement,ShadowRoot:window.ShadowRoot,setTimeout,clearTimeout};vm.createContext(context);
 vm.runInContext(fs.readFileSync(repo+'/static/vendor/vue-3.5.40.global.prod.js','utf8'),context);vm.runInContext(code,context);
 window.canExecute=canExecute;const ui=window.TMReplenishmentProduction,app=context.Vue.createApp({data:()=>({actor:1,generation:1,show:true,changed:0,locks:{},canExecute}),methods:{report(s){if(s.blocked)this.locks[s.identity]=true;else delete this.locks[s.identity];}},template:jobs?'<replenishment-production-jobs ref="jobs" :actor-id="actor" :auth-generation="generation" :can-execute="canExecute" :operation-reporter="report"/>':'<replenishment-production v-if="show" ref="source" :actor-id="actor" :auth-generation="generation" :source-id="41" product-label="80011641" :can-execute="canExecute" :operation-reporter="report" @changed="changed++"/>'});
 app.component('replenishment-production',ui.component);app.component('replenishment-production-jobs',ui.jobsComponent);app.component('location-picker',{props:['locations','modelValue','disabled'],template:'<div/>'});const parent=app.mount(window.document.querySelector('#app'));
 const tick=async()=>{for(let i=0;i<5;i++){await Promise.resolve();await context.Vue.nextTick();}};await tick();
 return {window,app,parent,ui,requests,store,tick,get source(){return parent.$refs.source;}};
}
test('stock source cannot be routed to sales order endpoint; preview then plan uses frozen input',async()=>{
 const h=await harness();try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);h.source.quantity=12;await h.source.save('sheet','plan');await h.tick();
 assert.equal(h.requests.length,2);assert.equal(h.requests[0].url,endpoint+'/preview');assert.equal(h.requests[1].url,endpoint);assert.equal(h.requests[1].payload.quantity,12);assert.equal(h.requests[1].payload.inventory_lot_id,9);assert.equal(h.requests[1].payload.lot_version,3);assert.ok(!('order_item_id' in h.requests[1].payload));assert.equal(h.parent.changed,1);assert.equal(h.ui.pending.size,0);assert.equal(h.store.length,0);
 }finally{h.app.unmount();}
});
test('lost write survives complete page reload and result receipt settles without another write',async()=>{
 const store=storage(),h=await harness({store,post:(url)=>{if(url.endsWith('/preview'))return {data:{preview_hash:'b'.repeat(64)}};throw Error('lost response');}});
 await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);await h.source.save('sheet','plan');const original=h.requests[1].payload;assert.equal(store.length,1);h.app.unmount();
 const fresh=await harness({store,post:(url,payload)=>{assert.equal(url,endpoint+'/result');assert.deepEqual(JSON.parse(JSON.stringify(payload.original_request)),original);return {data:{status:'completed',result:{message:'原回执'}}};}});
 try{assert.equal(fresh.source.uncertain,true);await fresh.source.open('jobs');await fresh.source.recover();assert.equal(fresh.requests.length,1);assert.equal(store.length,0);assert.equal(fresh.parent.changed,1);}finally{fresh.app.unmount();}
});
test('result not found resends identical request and key; edited form cannot alter retry',async()=>{
 let writes=0;const h=await harness({post:(url)=>{if(url.endsWith('/preview'))return {data:{preview_hash:'b'.repeat(64)}};if(url.endsWith('/result'))return {data:{status:'not_found'}};if(++writes===1)throw Error('offline');return {data:{message:'成功'}};}});
 try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);await h.source.save('sheet','plan');h.source.quantity=1;await h.source.recover();assert.deepEqual(h.requests[1],h.requests[3]);assert.equal(h.parent.changed,1);}finally{h.app.unmount();}
});
test('no replay while request in flight; detached request can settle via actor recovery',async()=>{
 const write=deferred(),h=await harness({post:(url)=>url.endsWith('/preview')?{data:{preview_hash:'b'.repeat(64)}}:url.endsWith('/result')?{data:{status:'completed',result:{}}}:write.promise});
 try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);const work=h.source.save('sheet','plan');await h.tick();assert.equal((await h.ui.reconcileForActor(1,()=>true,h.parent.report)).complete,false);assert.equal(h.requests.length,2);h.parent.show=false;await h.tick();write.reject(Error('lost'));await work;assert.equal(h.ui.pending.size,1);await h.ui.reconcileForActor(1,()=>true,h.parent.report);assert.equal(h.ui.pending.size,0);assert.equal(Object.keys(h.parent.locks).length,0);}finally{h.app.unmount();}
});
test('actor switch during preview prevents write and late read cannot populate new actor',async()=>{
 const preview=deferred(),h=await harness({post:()=>preview.promise});try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);const save=h.source.save('sheet','plan');h.parent.actor=2;h.parent.generation++;await h.tick();preview.resolve({data:{preview_hash:'x'}});await save;assert.equal(h.requests.length,1);assert.equal(h.source.detail,null);assert.equal(h.source.opened,false);assert.equal(h.parent.changed,0);assert.equal(h.ui.pending.size,0);}finally{h.app.unmount();}
 const read=deferred(),r=await harness({get:()=>read.promise});try{const open=r.source.open('sheet');r.parent.actor=2;r.parent.generation++;await r.tick();read.resolve({data:detail()});await open;assert.equal(r.source.detail,null);}finally{r.app.unmount();}
});
test('sheet completion records actual output and location/version; cancel confirms once and never completes',async()=>{
 const h=await harness();try{await h.source.open('jobs');const job={id:4,version:2,lot_version:8,expected_output:20,_output:18};await h.source.save('sheet','complete',job);assert.equal(h.requests[0].payload.actual_output,18);assert.equal(h.requests[0].payload.location_id,7);assert.equal(h.requests[0].payload.layout_version,5);assert.equal(h.requests[0].payload.lot_version,8);assert.equal(h.requests[0].payload.confirm_overproduction,false);
 h.window.confirm=()=>false;await h.source.save('sheet','cancel',job);assert.equal(h.requests.length,1);h.window.confirm=()=>true;await h.source.save('sheet','cancel',job);assert.equal(h.requests[1].payload.action,'cancel');assert.equal(h.requests[1].payload.lot_version,8);assert.equal(h.requests[1].payload.actual_output,undefined);
 }finally{h.app.unmount();}
});
test('scrap shares real replenishment source and immutable recovery, no sales order id',async()=>{
 let writes=0;const h=await harness({post:(url)=>{if(url.endsWith('/preview'))return {data:{preview_hash:'b'.repeat(64)}};if(++writes===1)throw Error('lost');return {data:{message:'done'}};}});
 try{await h.source.open('scrap');h.source.scrapQuantity=5;await h.source.save('scrap','plan');assert.equal(h.requests[1].payload.stock_replenishment_item_id,41);assert.ok(!('order_item_id' in h.requests[1].payload));h.source.scrapQuantity=6;await h.source.recover();assert.deepEqual(h.requests[1],h.requests[2]);assert.equal(h.parent.changed,1);}finally{h.app.unmount();}
});
test('known rejection clears hold but failed refresh after success never permits stale second submit',async()=>{
 const h=await harness({post:url=>{if(url.endsWith('/preview'))return {data:{preview_hash:'x'}};const e=Error('已产生采购，请先处理采购');e.response={status:409};throw e;}});
 try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);await h.source.save('sheet','plan');assert.equal(h.ui.pending.size,0);assert.equal(h.source.detail,null);assert.match(h.source.error,/采购/);assert.equal(h.parent.changed,0);}finally{h.app.unmount();}
 let reads=0;const r=await harness({get:url=>{if(url.endsWith('/locations'))return {data:{items:[]}};if(reads++)throw Error('read offline');return {data:detail()};}});
 try{await r.source.open('sheet');r.source.choose(r.source.detail.candidates[0]);await r.source.save('sheet','plan');assert.match(r.source.error,/操作已保存/);assert.equal(r.source.detail,null);await r.source.save('sheet','plan');assert.equal(r.requests.length,2);}finally{r.app.unmount();}
});
test('storage unavailable blocks mutation before write; read-only actor cannot submit',async()=>{
 const store=storage();store.setItem=()=>{throw Error('本机请求保存失败');};const h=await harness({store});try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);await h.source.save('sheet','plan');assert.equal(h.requests.length,1);assert.match(h.source.error,/保存失败/);assert.equal(h.ui.pending.size,0);}finally{h.app.unmount();}
});
test('production management remains visible for fully covered source independently of requisition rows',async()=>{
 const h=await harness({jobs:true,get:()=>({data:{items:[{id:8,stock_replenishment_item_id:41,product_code:'80011641',product_name:'D4/U-08B',source_kind:'sheet'},{id:9,stock_replenishment_item_id:41,source_kind:'scrap'}]}})});
 try{await h.tick();assert.equal(h.parent.$refs.jobs.rows.length,1);assert.match(h.window.document.body.textContent,/生产安排 \/ 记录/);assert.match(html,/<replenishment-production-jobs/);assert.match(html,/row.can_purchase===false/);assert.match(html,/:source-id="Number\(row.stock_replenishment_item_id\)"/);assert.ok(!code.includes('order-items/'));}finally{h.app.unmount();}
});
test('entry asset hash matches',()=>{
 const crypto=require('node:crypto'),hash=crypto.createHash('sha256').update(code).digest('hex').slice(0,12);assert.ok(html.includes('/static/ui/replenishment-production.js?v='+hash));
});

test('result endpoint rejection cannot discard a possibly committed original request',async()=>{
 const h=await harness({post:url=>{if(url.endsWith('/preview'))return {data:{preview_hash:'x'}};const e=Error('核对来源已变');if(url.endsWith('/result'))e.response={status:409};throw e;}});
 try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);await h.source.save('sheet','plan');const original=h.requests[1];await h.source.recover();assert.equal(h.ui.pending.size,1);assert.equal(h.store.length,1);assert.equal(h.requests.length,3);assert.deepEqual(h.requests[2].payload.original_request,original.payload);assert.equal(h.source.uncertain,true);assert.match(h.source.error,/未确认/);}finally{h.app.unmount();}
});
test('read-only actor cannot mutate and location/version is required to complete',async()=>{
 const h=await harness({canExecute:false});try{await h.source.open('sheet');h.source.choose(h.source.detail.candidates[0]);await h.source.save('sheet','plan');assert.equal(h.requests.length,0);}finally{h.app.unmount();}
 const r=await harness({get:url=>({data:url.endsWith('/locations')?{items:[]}:detail()})});try{await r.source.open('jobs');await r.source.save('sheet','complete',{id:4,version:1,lot_version:2,_output:20,expected_output:20});assert.equal(r.requests.length,0);assert.match(r.source.error,/有效的成品实际货位/);}finally{r.app.unmount();}
});
test('closed dialog discards late locations from its earlier read sequence',async()=>{
 const late=deferred(),h=await harness({get:url=>url.endsWith('/locations')?late.promise:{data:detail()}});
 try{const opening=h.source.open('jobs');await h.tick();h.source.close();late.resolve({data:{items:[{id:8,layout_version:9}]}});await opening;assert.equal(h.source.locations.length,0);}finally{h.app.unmount();}
});

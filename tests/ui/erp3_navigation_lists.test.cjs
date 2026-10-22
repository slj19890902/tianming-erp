const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=file=>process.env.ERP3_FRONTEND_BASELINE ? require('node:child_process').execFileSync('git',['show',`${process.env.ERP3_FRONTEND_BASELINE}:${file}`],{encoding:'utf8',maxBuffer:8*1024*1024}) : fs.readFileSync(file,'utf8');
const html=source('static/index.html');
const browser={};vm.runInNewContext(fs.readFileSync('static/js/list-read-state.js','utf8'),{window:browser});const runtime=vm.createContext({console});
vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),runtime);
const Vue=runtime.Vue;
const tick=()=>new Promise(setImmediate);
function methods(names,context={}) {
 const parts=names.map(name=>{const marker=new RegExp(`          (?:async )?${name}\\(`),start=html.search(marker);assert.ok(start>=0,name);return html.slice(start,html.indexOf('\n          },',start)+12);});
 return vm.runInNewContext(`({${parts.join(',')}})`,{AbortController,window:browser,...context});
}
function requests(){const pending=[];return {pending,axios:{get(url,config){return new Promise((resolve,reject)=>pending.push({url,config,resolve,reject}));}}};}
function list(){
 const r=requests(),controllers=new Map();
 const state=Vue.reactive({activePage:'products',productTab:'products',authGeneration:1,user:{id:1},selectedProductCustomer:{id:7,customer_code:'TEST'},
  filters:{},pages:{products:1},products:[{id:11,product_code:'TEST-OLD',product_name:'Old product',is_active:true,version:2}],productsTotal:1,productsLoading:false,productsError:'',listReadStates:{'products:list':{identity:'1:1:7',loaded:true,loading:false,error:''}},
  productListPageSize:()=>10,productLifecycleBusy:()=>false,errorMessage:e=>e.message,
  ...methods(['beginLatestRequest','finishLatestRequest','isCancelledRequest','loadProducts'],{axios:r.axios,latestRequestControllers:controllers})});
 return {...r,state,controllers};
}
test('page reading neither locks navigation nor clears a concurrent business write flag',async()=>{
 let finish;const state=Vue.reactive({activePage:'products',pageLoadSequence:0,authGeneration:1,user:{id:1},loading:false,pageLoading:false,productTab:'products',selectedProductCustomer:{id:7},
  pageCacheFresh:()=>false,invalidatePageCache(){},markPageCache(){},loadCustomerOptions:async()=>true,loadProducts:()=>new Promise(r=>finish=r),showToast(){},
  ...methods(['loadPage'])});
 const reading=state.loadPage('products');await tick();assert.equal(state.loading,false);assert.equal(state.pageLoading,true);
 state.loading=true;finish(true);await reading;assert.equal(state.loading,true);assert.equal(state.pageLoading,false);
});
test('older page completion cannot release or mark the newer page load',async()=>{
 const pending=[],cached=[];const state={activePage:'customers',pageLoadSequence:0,authGeneration:1,user:{id:1},loading:false,pageLoading:false,pageCacheFresh:()=>false,invalidatePageCache(){},markPageCache:p=>cached.push(p),
  loadCustomers:()=>new Promise(r=>pending.push(r)),loadOverview:()=>new Promise(r=>pending.push(r)),showToast(){},...methods(['loadPage'])};
 const old=state.loadPage('customers');state.activePage='dashboard';const current=state.loadPage('dashboard');pending[0](true);assert.equal(await old,false);assert.equal(state.pageLoading,true);assert.deepEqual(cached,[]);
 pending[1](true);assert.equal(await current,true);assert.equal(state.pageLoading,false);assert.deepEqual(cached,['dashboard']);
});
test('same-customer refresh keeps rows on read failure, while new customer and account discard them',async()=>{
 const f=list(),s=f.state;const refresh=s.loadProducts();assert.equal(s.products[0].id,11);f.pending[0].reject(new Error('offline'));await refresh;assert.equal(s.products[0].id,11);assert.equal(s.productsError,'offline');
 s.selectedProductCustomer={id:8};const next=s.loadProducts();assert.equal(s.products.length,0);f.pending[1].resolve({data:{items:[{id:22}],total:1}});await next;assert.equal(s.products[0].id,22);
 s.authGeneration=2;s.user={id:2};const account=s.loadProducts();assert.equal(s.products.length,0);f.pending[2].resolve({data:{items:[],total:0}});await account;
});
test('late product responses cannot repaint after leaving, changing customer, or changing actor',async()=>{
 for(const change of [s=>s.activePage='orders',s=>s.selectedProductCustomer={id:9},s=>{s.authGeneration++;s.user={id:2};}]){
  const f=list(),reading=f.state.loadProducts();change(f.state);f.pending[0].resolve({data:{items:[{id:999}],total:1}});assert.equal(await reading,false);assert.notEqual(f.state.products[0]?.id,999);
 }
 const f=list();const older=f.state.loadProducts(),newer=f.state.loadProducts();assert.equal(f.pending[0].config.signal.aborted,true);f.pending[1].resolve({data:{items:[{id:22}],total:1}});await newer;f.pending[0].resolve({data:{items:[{id:999}],total:1}});await older;assert.equal(f.state.products[0].id,22);
});
function renderProductList(state){
 // Execute the real shipped Vue compiler/render pipeline against an in-memory host.
 // Browser geometry is verified separately in isolated Chrome, not faked here.
 const start=html.indexOf('                  <list-read-status');
 const end=html.indexOf('\n                </template>',start);assert.ok(start>=0&&end>start);
 const template='<section>'+html.slice(start,end)+'</section>';
 const node=(tag,text='')=>({tag,text,children:[],props:{},parent:null});
 const renderer=Vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),setText:(n,t)=>n.text=t,setElementText:(n,t)=>{n.text=t;n.children=[];},
  insert(n,p,anchor){if(n.parent){n.parent.children.splice(n.parent.children.indexOf(n),1);}n.parent=p;const i=anchor?p.children.indexOf(anchor):-1;if(i<0)p.children.push(n);else p.children.splice(i,0,n);},remove(n){if(n.parent)n.parent.children.splice(n.parent.children.indexOf(n),1);},parentNode:n=>n.parent,nextSibling:n=>n.parent?.children[n.parent.children.indexOf(n)+1],patchProp:(n,k,o,v)=>n.props[k]=v});
 const data=Object.assign(state,{canViewSalesAmounts:true,canEditProducts:true,canRequisition:true,canEditStockAlerts:true,canDeactivateProducts:true,canDeleteProducts:true,
  plainProductText:x=>x||'',productUnitLabel:()=> '片',spec:()=> '100×80',productMaterialCell:()=>({text:'材料'}),materialFullStructure:()=> '材料',unitPrice:()=> '1.00',commonBoxPriceLabel:()=> '单价',commonBoxReadiness:()=>({ready:false}),commonBoxReadinessTitle:()=> '缺材质',productLifecyclePending:()=>false,
  positionProductRowActions(){},closeProductRowActions(){},leaveProductRowActions(){},openProduct(){},previewProductDrawing(){},openProductStockPolicy(){},openMasterVersionHistory(){},toggleProductStatus(){},deleteProduct(){},runExplicitPageListLoad(){}});
 const root=node('root'),app=renderer.createApp({setup:()=>data,render:Vue.compile(template,{decodeEntities:s=>s})});
 app.component('list-read-status',{...browser.ERPListRead.component,render:Vue.compile(browser.ERPListRead.component.template,{decodeEntities:s=>s})});app.mount(root);
 const all=(tag,n=root)=>[...(n.tag===tag?[n]:[]),...n.children.flatMap(c=>all(tag,c))];
 const text=(n=root)=>n.text+n.children.map(c=>text(c)).join('');
 return {root,all,app,data,text};
}
test('actual Vue list retains table through refresh/error, and keeps permission-bound auxiliary actions outside the table cell layout',async()=>{
 const f=list(),h=renderProductList(f.state);const table=h.all('table')[0];assert.ok(table);
 f.state.listReadStates['products:list'].loading=true;await Vue.nextTick();assert.equal(h.all('table')[0],table);assert.match(h.text(),/正在更新，以下保留上次结果/);
 f.state.listReadStates['products:list'].loading=false;f.state.listReadStates['products:list'].error='offline';await Vue.nextTick();assert.equal(h.all('table')[0],table);assert.match(h.text(),/更新失败，以下保留上次结果：offline/);
 const cells=h.all('td');assert.ok(cells.every(c=>c.props.class!=='toolbar-group'));
 assert.equal(h.all('summary').length,1);assert.equal(h.all('details').length,1);
 f.state.canDeactivateProducts=false;f.state.canDeleteProducts=false;await Vue.nextTick();assert.doesNotMatch(h.text(),/移入垃圾站|停用/);
 f.state.products=[];await Vue.nextTick();assert.equal(h.all('table').length,0);assert.match(h.text(),/读取失败：offline/);assert.doesNotMatch(h.text(),/该客户当前没有符合条件/);
 f.state.listReadStates['products:list'].error='';await Vue.nextTick();assert.match(h.text(),/该客户当前没有符合条件的常用箱/);h.app.unmount();
});

test('page-scoped GETs cancel and reject stale success, while mutation guards follow the original request',()=>{
 let request,success,failure;const window={erpRequestIdentity:()=>({actor:1,generation:1}),erpPageReadIdentity:()=>({page:'orders',sequence:1,reading:true})};
 const axios={defaults:{},CanceledError:class extends Error{constructor(m){super(m);this.code='ERR_CANCELED';}},interceptors:{request:{use(fn){request=fn;}},response:{use(ok,fail){success=ok;failure=fail;}}}};
 const source=html.slice(html.indexOf('      axios.defaults.withCredentials'),html.indexOf('      const { createApp } = Vue;'));
 vm.runInNewContext(source,{window,axios,AbortController,Promise});
 const get=request({url:'/api/orders',method:'get'});assert.equal(window.erpMutationPending(),false);
 const write=request({url:'/api/requisition/holds/auto-release',method:'post'});assert.equal(window.erpMutationPending(),true);
 window.erpRequestIdentity=()=>({actor:1,generation:2});assert.equal(window.erpMutationPending(),false);
 window.erpRequestIdentity=()=>({actor:2,generation:1});assert.equal(window.erpMutationPending(),false);
 window.erpRequestIdentity=()=>({actor:1,generation:1});assert.equal(window.erpMutationPending(),true);
 window.erpCancelPageReads();assert.equal(get.signal.aborted,true);assert.equal(write.signal,undefined);
 window.erpPageReadIdentity=()=>({page:'products',sequence:2,reading:true});assert.throws(()=>success({config:get,data:{items:['OLD']}}),e=>e.code==='ERR_CANCELED');
 success({config:write});assert.equal(window.erpMutationPending(),false);
 const failing=request({method:'post'});const result=failure({config:failing});assert.equal(window.erpMutationPending(),false);return result.catch(()=>{});
});

test('product viewport is measured once per geometry, not on taller results or failed refresh',async()=>{
 const mixins=[],dimensions={row:44,count:8},calls=[];
 const main={scrollTop:0,getBoundingClientRect:()=>({bottom:720})};
 const table={parentElement:main,nextElementSibling:null,closest:()=>main,getBoundingClientRect:()=>({top:180,width:1400,height:dimensions.count*dimensions.row+38}),
  querySelectorAll:()=>Array.from({length:dimensions.count},()=>({cells:[1,2],getBoundingClientRect:()=>({height:dimensions.row})})),tHead:{getBoundingClientRect:()=>({height:38})}};
 const window={innerWidth:1080,innerHeight:720};const document={querySelector:selector=>selector.startsWith('[data-viewport')?table:null};
 vm.runInNewContext(source('static/ui/workspace.js'),{window,document,getComputedStyle:()=>({})});
 window.ERPWorkspace.install({mixin:m=>mixins.push(m),component(){}});
 const s={activePage:'products',productTab:'products',selectedProductCustomer:{id:7},uiMode:'standard',workspaceFitSizes:{},pages:{products:1},productListPageSize:()=>50,
  runExplicitPageListLoad:async()=>calls.push(s.pages.products),...mixins[0].methods,queueViewportPageMeasure(){}};
 await s.measureViewportPage();assert.equal(calls.length,1);s.pages.products=3;dimensions.row=100;dimensions.count=1;
 await s.measureViewportPage();assert.equal(calls.length,1);assert.equal(s.pages.products,3);
 s.productsError='offline';window.innerHeight=640;await s.measureViewportPage();assert.equal(calls.length,1);
 s.productsError='';await s.measureViewportPage();assert.equal(calls.length,2);await s.measureViewportPage();assert.equal(calls.length,2);
});

test('cancelled current product read releases its indicator but older finally cannot clear a newer read',async()=>{
 const f=list(),first=f.state.loadProducts();f.controllers.get('products:list').abort();f.state.activePage='orders';
 f.pending[0].reject(Object.assign(new Error('cancelled'),{code:'ERR_CANCELED'}));await first;assert.equal(f.state.productsLoading,false);
 f.state.activePage='products';const old=f.state.loadProducts(),current=f.state.loadProducts();
 f.pending[1].reject(Object.assign(new Error('cancelled'),{code:'ERR_CANCELED'}));await old;assert.equal(f.state.productsLoading,true);
 f.pending[2].resolve({data:{items:[],total:0}});await current;assert.equal(f.state.productsLoading,false);
});

test('cancelled initial customer options cannot continue an obsolete products or quotation page',async()=>{
 for(const page of ['products','quotations']){
  let finish;const calls=[];const state={activePage:page,pageLoadSequence:0,authGeneration:1,user:{id:1},productTab:'products',selectedProductCustomer:{id:7},quotationCustomer:7,
   pageCacheFresh:()=>false,invalidatePageCache(){},markPageCache:p=>calls.push(p),loadCustomerOptions:()=>new Promise(r=>finish=r),
   loadProducts:()=>calls.push('products'),loadQuotationHistory:()=>calls.push('quotations'),...methods(['loadPage'])};
  const old=state.loadPage(page);state.activePage='orders';state.pageLoadSequence++;finish(false);assert.equal(await old,false);assert.deepEqual(calls,[]);
 }
});

test('obsolete initialization cannot open location dialogs after navigation or refresh',async()=>{
 for(const supersede of [s=>s.activePage='orders',s=>s.pageLoadSequence++]){
  let finish;const dialogs=[];const state={activePage:'products',authGeneration:1,user:{id:1},pageLoadSequence:0,loadSuppliers:async()=>true,
   loadPage(){this.pageLoadSequence++;return new Promise(r=>finish=r);},...methods(['loadInitialPageResources'],{window:{TMManualReplenishment:{openFromLocation:async()=>dialogs.push('opened')}}})};
  const initial=state.loadInitialPageResources();supersede(state);finish(true);assert.equal(await initial,false);assert.deepEqual(dialogs,[]);
 }
});

test('workspace layout rules are in the actual document head and stay out of the print template',()=>{
 const head=html.slice(0,html.indexOf('</head>'));
 assert.match(head,/html\.frontend-shell-layout body\s*\{\s*min-width:0/);
 assert.match(head,/\.product-row-actions\s*\{[^}]*flex-wrap:nowrap/);
 const printStart=html.indexOf('w.document.write(`<html><head><title>供应商报料单');
 assert.ok(printStart>0);assert.doesNotMatch(html.slice(printStart,html.indexOf('w.document.close();',printStart)),/product-row-actions|frontend-shell-layout/);
});

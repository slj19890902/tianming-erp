'use strict';
// Actual SFC script functions with explicit Vue/API/DOM doubles. No browser or
// server runs here; viewport assertions validate calculations, not appearance.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const test=require('node:test'),assert=require('node:assert/strict');
const ts=require(process.env.ERP_UI_TYPESCRIPT_LIBRARY);
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/views/OrdersWorkspaceView.vue'),'utf8');
const priorRoot=process.env.ERP_UI_FIXED_BASE;
if(!priorRoot)throw Error('ERP_UI_FIXED_BASE must point to immutable prior source');
const prior=fs.readFileSync(path.join(priorRoot,'frontend-v2/src/views/OrdersWorkspaceView.vue'),'utf8');
const script=source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
const exposed=['rows','total','page','pageSize','keyword','scope','loading','detailLoading','detail','detailVisible',
  'editing','saving','editForm','loadError','hasSuccessfulLoad','lastSuccessfulQuery','failedQuery','listCard',
  'tableHost','paginationHost','tableHeight','loadOrders','retryOrders','search','openDetail','saveBasicInfo',
  'currentQuery','describeQuery','changePage','calculateTableHeight','measureTableHeight','scheduleTableMeasure',
  'startTableMeasurements','stopTableMeasurements'];
exposed.push('openOriginalEntry','canViewSalesAmounts');
const entrySource=fs.readFileSync(path.join(root,'src/utils/formalOrderEntry.ts'),'utf8');
const entryExports={};
vm.runInNewContext(ts.transpileModule(entrySource,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:entryExports,crypto:require('node:crypto').webcrypto});
const compiled=ts.transpileModule(script+'\nglobalThis.testApi={'+exposed.join(',')+'};',{
  reportDiagnostics:true,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},
});
assert.equal(compiled.diagnostics.filter(d=>d.category===ts.DiagnosticCategory.Error).length,0);
const fixture={id:1,order_number:'SYNTHETIC-001',customer_name:'合成客户甲',total_quantity:100,
  business_remaining_quantity:20,item_count:1};
const fullOrder={...fixture,customer_po:'PO-TEST',delivery_date:'2026-10-03',remark:'合成备注',items:[]};
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
async function settle(predicate){for(let n=0;n<40;n++){if(predicate())return;await new Promise(setImmediate)}throw Error('not settled')}
function harness(){
  const hooks={mounted:[],activated:[],deactivated:[],unmounted:[],watch:[]},calls=[],messages=[],frames=new Map(),listeners=new Map(),observers=[];
  let frameId=0;
  const impl={list:async()=>({items:[fixture],total:1}),get:async()=>({...fullOrder}),update:async()=>({})};
  const orderApi={listOrders(...args){calls.push(['list',...args]);return impl.list(...args)},
    getOrder(...args){calls.push(['get',...args]);return impl.get(...args)},updateOrder(...args){calls.push(['update',...args]);return impl.update(...args)}};
  const vue={ref:value=>({value}),computed:fn=>({get value(){return fn()}}),nextTick:fn=>Promise.resolve().then(()=>fn?.()),
    onMounted:fn=>hooks.mounted.push(fn),onActivated:fn=>hooks.activated.push(fn),
    onDeactivated:fn=>hooks.deactivated.push(fn),onBeforeUnmount:fn=>hooks.unmounted.push(fn),
    watch:(input,callback,options)=>{hooks.watch.push({input,callback,options});if(options?.immediate&&typeof input==='function')callback(input())}};
  const route={path:'/review/orders',query:{}};
  const auth={user:{role:'sales'},hasPermission:permission=>permission==='orders.edit'};
  const context={exports:{},Error,Number,Promise,
    window:{innerHeight:768,addEventListener:(name,fn)=>listeners.set(name,fn),
      removeEventListener:(name,fn)=>{if(listeners.get(name)===fn)listeners.delete(name)},
      requestAnimationFrame:fn=>{frames.set(++frameId,fn);return frameId},cancelAnimationFrame:id=>frames.delete(id)},
    ResizeObserver:class {constructor(fn){this.fn=fn;this.targets=[];this.disconnected=false;observers.push(this)}observe(target){this.targets.push(target)}disconnect(){this.disconnected=true}},
    require(id){if(id==='vue')return vue;if(id==='vue-router')return {useRoute:()=>route,useRouter:()=>({push:url=>calls.push(['route',url])})};
      if(id==='element-plus')return {ElMessage:Object.fromEntries(['error','warning','success'].map(kind=>[kind,text=>messages.push([kind,text])]))};
      if(id==='../api/client')return {orderApi};if(id==='../stores/auth')return {useAuthStore:()=>auth};
      if(id==='../utils/formalOrderEntry')return entryExports;
      throw Error('unexpected import '+id)},
  };
  vm.createContext(context);vm.runInContext(compiled.outputText,context,{timeout:1000});
  function flush(){const current=[...frames];frames.clear();for(const [,fn] of current)fn()}
  return {api:context.testApi,context,hooks,calls,messages,impl,listeners,frames,observers,route,flush,auth};
}
function attachTable(h,top=190,footer=32){
  const host={isConnected:true,visible:true,getClientRects(){return this.visible?[{}]:[]},getBoundingClientRect:()=>({top})};
  h.api.tableHost.value=host;h.api.paginationHost.value={getBoundingClientRect:()=>({height:footer})};h.api.listCard.value={};return host;
}

test('native entries use complete original forms with create and email-role guards',()=>{
  const h=harness();
  h.api.openOriginalEntry('new');assert.equal(h.calls.length,0);
  h.auth.hasPermission=()=>true;
  h.api.openOriginalEntry('new');h.api.openOriginalEntry('import');h.api.openOriginalEntry('email');
  assert.equal(h.calls.length,2);
  assert.equal(h.calls[0][1].path,'/orders');assert.equal(h.calls[0][1].query.order_entry,'new');
  assert.equal(h.calls[1][1].query.order_entry,'import');
  assert.equal(entryExports.parseFormalOrderEntry(h.calls[0][1].query.order_entry,h.calls[0][1].query.order_request).action,'new');
  h.auth.user.role='admin';h.api.openOriginalEntry('email');assert.equal(h.calls[2][1].query.order_entry,'email');
});

test('original save refreshes committed query on activation, preserving draft filters and edits',async()=>{
 const h=harness();h.auth.user.id=1;h.auth.hasPermission=()=>true;
 for(const fn of h.hooks.mounted)fn();await settle(()=>!h.api.loading.value);
 h.api.keyword.value='draft-unsent';h.api.editing.value=true;h.api.editForm.value.remark='keep-edit';h.route.path='/orders';
 const listener=h.listeners.get('tianming-orders-changed'),before=h.calls.length;
 listener({detail:{actorId:2}});listener({detail:{actorId:1}});assert.equal(h.calls.length,before);
 h.route.path='/review/orders';for(const fn of h.hooks.activated)fn();await settle(()=>!h.api.loading.value);
 assert.equal(h.calls.at(-1)[3].keyword,'');assert.equal(h.api.keyword.value,'draft-unsent');assert.equal(h.api.editing.value,true);assert.equal(h.api.editForm.value.remark,'keep-edit');
 for(const fn of h.hooks.unmounted)fn();assert.equal(h.listeners.has('tianming-orders-changed'),false);
});

test('sales amount visibility retains original role and order-view conditions',()=>{
  const h=harness();h.auth.hasPermission=()=>true;
  assert.equal(h.api.canViewSalesAmounts.value,true);
  h.auth.user.role='workshop';assert.equal(h.api.canViewSalesAmounts.value,false);
  h.auth.user.role='admin';h.auth.hasPermission=()=>false;assert.equal(h.api.canViewSalesAmounts.value,false);
});
test('successful list commits rows, server total and exact pageSize20 parameters',async()=>{
  const h=harness();h.api.page.value=3;h.api.keyword.value='  SYNTHETIC  ';h.api.scope.value='completed';
  await h.api.loadOrders();assert.equal(h.api.rows.value[0],fixture);assert.equal(h.api.total.value,1);
  assert.equal(h.api.hasSuccessfulLoad.value,true);assert.equal(h.api.loadError.value,'');
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0])),['list',3,20,{keyword:'SYNTHETIC',scope:'completed'}]);
});
test('successful empty list is known empty, with explicit empty slot distinct from first failure',async()=>{
  const h=harness();h.impl.list=async()=>({items:[],total:0});await h.api.loadOrders();
  assert.equal(h.api.hasSuccessfulLoad.value,true);assert.equal(h.api.rows.value.length,0);assert.equal(h.api.loadError.value,'');
  assert.match(source,/v-else-if="hasSuccessfulLoad">未找到符合当前筛选条件的订单/);
});
test('first failure leaves success unknown and never displays default no-data as a successful result',async()=>{
  const h=harness();h.impl.list=async()=>{throw Error('SYNTHETIC offline')};await h.api.loadOrders();
  assert.equal(h.api.hasSuccessfulLoad.value,false);assert.equal(h.api.rows.value.length,0);
  assert.equal(h.api.loadError.value,'SYNTHETIC offline');assert.equal(h.api.loading.value,false);
  assert.match(source,/尚未取得订单结果，不能判断是否存在匹配订单/);assert.match(source,/订单读取失败，尚无可显示结果，请重试/);
});
test('failed later query keeps the exact prior rows, total and successful query description',async()=>{
  const h=harness();await h.api.loadOrders();const rows=h.api.rows.value;
  h.api.page.value=2;h.api.keyword.value='new';h.impl.list=async()=>{throw Error('SYNTHETIC fail')};await h.api.loadOrders();
  assert.equal(h.api.rows.value,rows);assert.equal(h.api.total.value,1);assert.equal(h.api.lastSuccessfulQuery.value.page,1);
  assert.equal(h.api.failedQuery.value.page,2);assert.equal(h.api.failedQuery.value.keyword,'new');
  assert.match(source,/当前保留上次成功结果/);assert.match(h.api.describeQuery(h.api.lastSuccessfulQuery.value),/第1页.*进行中.*无关键词/);
});
test('retry snapshots failed page/keyword/scope despite unsent controls, then clears error',async()=>{
  const h=harness();h.api.page.value=4;h.api.keyword.value=' ABC ';h.api.scope.value='cancelled';
  h.impl.list=async()=>{throw Error('SYNTHETIC fail')};await h.api.loadOrders();
  h.api.keyword.value='unsent';h.api.scope.value='all';h.impl.list=async()=>({items:[fixture],total:1});
  h.api.retryOrders();await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',4,20,{keyword:'ABC',scope:'cancelled'}]);
  assert.equal(h.api.loadError.value,'');assert.equal(h.api.failedQuery.value,null);assert.equal(h.api.hasSuccessfulLoad.value,true);
});
test('retry during an outstanding request does not issue another API call',async()=>{
  const h=harness();h.impl.list=async()=>{throw Error('fail')};await h.api.loadOrders();
  const pending=deferred();h.impl.list=()=>pending.promise;h.api.retryOrders();const n=h.calls.length;h.api.retryOrders();
  assert.equal(h.calls.length,n);pending.resolve({items:[fixture],total:1});await settle(()=>!h.api.loading.value);
});
test('old success cannot cover a newer successful response',async()=>{
  const h=harness(),a=deferred(),b=deferred();let n=0;h.impl.list=()=>++n===1?a.promise:b.promise;
  const oldRequest=h.api.loadOrders();h.api.page.value=2;const newest=h.api.loadOrders();
  b.resolve({items:[{...fixture,id:2}],total:50});await newest;a.resolve({items:[fixture],total:1});await oldRequest;
  assert.equal(h.api.rows.value[0].id,2);assert.equal(h.api.total.value,50);assert.equal(h.api.lastSuccessfulQuery.value.page,2);
});
test('old failure cannot replace a newer success with an error',async()=>{
  const h=harness(),a=deferred();let n=0;h.impl.list=()=>++n===1?a.promise:Promise.resolve({items:[fixture],total:1});
  const pending=h.api.loadOrders();await h.api.loadOrders();a.reject(Error('old failure'));await pending;
  assert.equal(h.api.loadError.value,'');assert.equal(h.api.failedQuery.value,null);assert.equal(h.messages.length,0);
});
test('old success cannot erase a newer failure or authorize a false current result',async()=>{
  const h=harness();await h.api.loadOrders();const rows=h.api.rows.value,a=deferred();let n=0;
  h.impl.list=()=>++n===1?a.promise:Promise.reject(Error('new failure'));
  const pending=h.api.loadOrders();h.api.page.value=7;await h.api.loadOrders();a.resolve({items:[{...fixture,id:999}],total:999});await pending;
  assert.equal(h.api.rows.value,rows);assert.equal(h.api.total.value,1);assert.equal(h.api.loadError.value,'new failure');assert.equal(h.api.failedQuery.value.page,7);
});
test('stale finally cannot remove loading flag while latest request remains pending',async()=>{
  const h=harness(),a=deferred(),b=deferred();let n=0;h.impl.list=()=>++n===1?a.promise:b.promise;
  const first=h.api.loadOrders(),last=h.api.loadOrders();a.resolve({items:[],total:0});await first;
  assert.equal(h.api.loading.value,true);b.resolve({items:[fixture],total:1});await last;assert.equal(h.api.loading.value,false);
});
test('search retains server flow and starts page one with current scope',async()=>{
  const h=harness();h.api.page.value=5;h.api.scope.value='all';h.api.search();await settle(()=>!h.api.loading.value);
  assert.equal(h.calls[0][1],1);assert.equal(h.calls[0][2],20);assert.equal(h.calls[0][3].scope,'all');
});
test('editing succeeds only after update/get strict readback and then reloads list',async()=>{
  const h=harness();await h.api.openDetail(1);h.api.editing.value=true;
  h.api.editForm.value={customer_po:' NEW ',delivery_date:'2026-10-04',remark:' NOTE '};
  h.impl.get=async()=>({...fullOrder,customer_po:'NEW',delivery_date:'2026-10-04',remark:'NOTE'});
  await h.api.saveBasicInfo();assert.deepEqual(h.calls.map(c=>c[0]),['get','update','get','list']);
  assert.equal(h.api.editing.value,false);assert.equal(h.api.saving.value,false);assert.equal(h.api.detail.value.customer_po,'NEW');
});
for(const field of ['customer_po','delivery_date','remark'])test(`strict edit ${field} mismatch stays editing and does not reload list`,async()=>{
  const h=harness();await h.api.openDetail(1);h.api.editing.value=true;
  h.impl.get=async()=>({...fullOrder,[field]:'SYNTHETIC mismatch'});await h.api.saveBasicInfo();
  assert.equal(h.api.editing.value,true);assert.ok(!h.calls.some(c=>c[0]==='list'));assert.equal(h.messages.at(-1)[0],'warning');
});
test('edit update failure keeps prior detail and cannot send a list refresh',async()=>{
  const h=harness();await h.api.openDetail(1);h.api.editing.value=true;const priorDetail=h.api.detail.value;
  h.impl.update=async()=>{throw Error('SYNTHETIC update fail')};await h.api.saveBasicInfo();
  assert.equal(h.api.detail.value,priorDetail);assert.equal(h.api.editing.value,true);assert.equal(h.api.saving.value,false);
  assert.deepEqual(h.calls.map(c=>c[0]),['get','update']);
});
test('1366x768 target height and resize use measured table top/footer, with zero business requests',async()=>{
  const h=harness();attachTable(h);h.api.startTableMeasurements();await Promise.resolve();h.flush();
  assert.equal(h.api.tableHeight.value,518);assert.equal(h.calls.length,0);
  h.context.window.innerHeight=900;h.listeners.get('resize')();h.flush();assert.equal(h.api.tableHeight.value,650);
  h.observers[0].fn();h.flush();assert.equal(h.calls.length,0);
});
test('hidden KeepAlive view is not measured; reactivation restores page state and height without reload',async()=>{
  const h=harness(),host=attachTable(h);await h.api.loadOrders();h.api.page.value=3;h.api.keyword.value='kept';h.api.scope.value='completed';h.api.detailVisible.value=true;
  h.api.startTableMeasurements();await Promise.resolve();h.flush();const calls=h.calls.length,rows=h.api.rows.value;
  h.hooks.deactivated[0]();host.visible=false;h.context.window.innerHeight=600;h.api.measureTableHeight();assert.equal(h.api.tableHeight.value,518);
  host.visible=true;h.hooks.activated[0]();await Promise.resolve();h.flush();assert.equal(h.api.tableHeight.value,350);
  assert.equal(h.calls.length,calls);assert.equal(h.api.rows.value,rows);assert.equal(h.api.page.value,3);assert.equal(h.api.keyword.value,'kept');assert.equal(h.api.scope.value,'completed');assert.equal(h.api.detailVisible.value,true);
});
test('KeepAlive error/retry snapshot is preserved; lifecycle cancels old frames and does not duplicate listeners',async()=>{
  const h=harness();attachTable(h);h.impl.list=async()=>{throw Error('SYNTHETIC error')};await h.api.loadOrders();
  const failed=h.api.failedQuery.value;h.api.startTableMeasurements();await Promise.resolve();assert.equal(h.frames.size,1);
  h.hooks.deactivated[0]();assert.equal(h.frames.size,0);assert.equal(h.listeners.size,0);assert.equal(h.observers[0].disconnected,true);
  h.hooks.activated[0]();h.hooks.activated[0]();await Promise.resolve();h.flush();
  assert.equal(h.listeners.size,1);assert.equal(h.api.failedQuery.value,failed);assert.equal(h.api.loadError.value,'SYNTHETIC error');
  assert.equal(h.calls.length,1);h.hooks.unmounted[0]();assert.equal(h.listeners.size,0);
});
test('short viewport minimum remains usable; detached/invalid layout leaves height unchanged',async()=>{
  const h=harness(),host=attachTable(h,NaN);h.api.startTableMeasurements();await Promise.resolve();h.flush();assert.equal(h.api.tableHeight.value,480);
  assert.equal(h.api.calculateTableHeight(300,250,32),240);host.isConnected=false;h.api.measureTableHeight();assert.equal(h.api.tableHeight.value,480);
});
test('shared list fields follow formal order; supplemental quantities stay explicit; detail/save remain unchanged',()=>{
  const columns=text=>text.split('<el-dialog')[0].match(/<vxe-column[^>]+>/g);
  assert.equal(columns(source).length,10);
  assert.deepEqual(columns(source).slice(0,7).map(col=>col.match(/field="([^"]+)"/)[1]),['customer_name','customer_po','order_date','delivery_date','total_quantity','total_amount','business_status_label']);
  assert.match(source,/row.customer_po \|\| '未填写客户单号'/);assert.match(source,/ERP \{\{ row.order_number \}\}/);
  assert.match(source,/<vxe-column v-if="canViewSalesAmounts" field="total_amount"/);
  const dialog=text=>text.slice(text.indexOf('<el-dialog'),text.indexOf('</el-dialog>')+12).replace(/\r\n/g,'\n');
  const drawingColumn='<vxe-column title="图纸" width="105" fixed="right"><template #default="{ row }"><el-button v-if="row.drawing_file || row.product_drawing_file" link @click="drawingPreviewSource = row.drawing_file || row.product_drawing_file">查看图纸</el-button><span v-else>—</span></template></vxe-column>';
  assert.equal(source.includes(drawingColumn),true);
  assert.equal(dialog(source).replace('          '+drawingColumn+'\n',''),dialog(prior));
  function getFunction(text,name){const raw=text.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];const ast=ts.createSourceFile('view.ts',raw,ts.ScriptTarget.Latest,true);return ast.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name.text===name).getText(ast).replace(/\r\n/g,'\n')}
  for(const name of ['openDetail','saveBasicInfo','search'])assert.equal(getFunction(source,name),getFunction(prior,name));
  const style=source.match(/<style scoped>([\s\S]*?)<\/style>/)[1];assert.doesNotMatch(style,/display:\s*none|overflow-x:\s*hidden/);
  assert.match(source,/:cell-config="\{ height: 56 \}"/);assert.match(source,/@current-change="changePage"/);
  assert.match(source,/auth.hasPermission\('orders.edit'\)/);assert.match(source,/数量沿用客户单据口径/);
});

test('pagination uses the committed query while a keyword draft remains unsubmitted',async()=>{
  const h=harness();h.api.keyword.value='OLD';await h.api.loadOrders();h.api.keyword.value='NEW';
  h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'OLD',scope:'active'}]);
  assert.equal(h.api.keyword.value,'NEW');assert.equal(h.api.lastSuccessfulQuery.value.keyword,'OLD');assert.equal(h.api.page.value,2);
});
test('pagination to page one also retains the committed query instead of submitting a draft',async()=>{
  const h=harness();h.api.keyword.value='OLD';h.api.page.value=3;await h.api.loadOrders();h.api.keyword.value='NEW';
  h.api.changePage(1);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',1,20,{keyword:'OLD',scope:'active'}]);
});
test('first query failure has no committed result; paging stays blocked and retry uses the first query',async()=>{
  const h=harness();h.api.keyword.value='OLD';h.api.scope.value='completed';h.impl.list=async()=>{throw Error('first failed')};
  await h.api.loadOrders();const n=h.calls.length;h.api.keyword.value='NEW';h.api.scope.value='all';h.api.changePage(2);
  assert.equal(h.calls.length,n);assert.equal(h.api.lastSuccessfulQuery.value,null);assert.equal(h.api.page.value,1);
  h.impl.list=async()=>({items:[fixture],total:51});h.api.retryOrders();await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',1,20,{keyword:'OLD',scope:'completed'}]);
  h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'OLD',scope:'completed'}]);
});
test('paging is blocked while the first read or a later page is loading',async()=>{
  const h=harness(),first=deferred();h.impl.list=()=>first.promise;const initial=h.api.loadOrders();
  h.api.changePage(2);assert.equal(h.calls.length,1);assert.equal(h.api.page.value,1);
  first.resolve({items:[fixture],total:51});await initial;
  const later=deferred();h.impl.list=()=>later.promise;h.api.changePage(2);const n=h.calls.length;h.api.changePage(3);
  assert.equal(h.calls.length,n);assert.equal(h.api.page.value,2);later.resolve({items:[fixture],total:51});await settle(()=>!h.api.loading.value);
});
test('failed later page blocks paging until fixed-query retry succeeds despite new controls',async()=>{
  const h=harness();h.api.keyword.value='OLD';await h.api.loadOrders();h.impl.list=async()=>{throw Error('page failed')};
  h.api.changePage(2);await settle(()=>!h.api.loading.value);const n=h.calls.length;h.api.keyword.value='NEW';h.api.changePage(3);
  assert.equal(h.calls.length,n);assert.equal(h.api.page.value,2);assert.equal(h.api.lastSuccessfulQuery.value.page,1);
  h.impl.list=async()=>({items:[fixture],total:51});h.api.retryOrders();await settle(()=>!h.api.loading.value);h.api.changePage(3);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',3,20,{keyword:'OLD',scope:'active'}]);
});
test('late old search success cannot change the query used by subsequent pagination',async()=>{
  const h=harness(),old=deferred();let n=0;h.impl.list=()=>++n===1?old.promise:Promise.resolve({items:[{...fixture,id:2}],total:51});
  h.api.keyword.value='OLD';h.api.search();h.api.keyword.value='NEW';h.api.search();await settle(()=>!h.api.loading.value);
  old.resolve({items:[fixture],total:999});await new Promise(setImmediate);h.api.keyword.value='unsent';h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'NEW',scope:'active'}]);
  assert.equal(h.api.rows.value[0].id,2);assert.equal(h.api.total.value,51);assert.equal(h.api.loadError.value,'');
});
test('late old search failure cannot disable paging for the newer successful query',async()=>{
  const h=harness(),old=deferred();let n=0;h.impl.list=()=>++n===1?old.promise:Promise.resolve({items:[fixture],total:51});
  h.api.keyword.value='OLD';h.api.search();h.api.keyword.value='NEW';h.api.search();await settle(()=>!h.api.loading.value);
  old.reject(Error('old failure'));await new Promise(setImmediate);h.api.keyword.value='unsent';h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'NEW',scope:'active'}]);
  assert.equal(h.api.loadError.value,'');assert.equal(h.api.failedQuery.value,null);assert.equal(h.messages.length,0);
});
test('new search failure remains retryable when an older success arrives late; retry commits new scope for paging',async()=>{
  const h=harness();h.api.keyword.value='OLD';await h.api.loadOrders();const old=deferred();let n=0;
  h.impl.list=()=>++n===1?old.promise:Promise.reject(Error('new failure'));h.api.changePage(2);
  h.api.keyword.value='NEW';h.api.scope.value='completed';h.api.search();await settle(()=>!h.api.loading.value);
  old.resolve({items:[{...fixture,id:999}],total:999});await new Promise(setImmediate);const calls=h.calls.length;
  h.api.changePage(2);assert.equal(h.calls.length,calls);assert.equal(h.api.failedQuery.value.keyword,'NEW');assert.equal(h.api.loadError.value,'new failure');
  h.api.keyword.value='unsent';h.api.scope.value='all';h.impl.list=async()=>({items:[fixture],total:51});
  h.api.retryOrders();await settle(()=>!h.api.loading.value);h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'NEW',scope:'completed'}]);
});
test('clearing and explicit searching commit page one; subsequent paging keeps the newly committed filter',async()=>{
  const h=harness();h.api.keyword.value='OLD';h.api.page.value=3;await h.api.loadOrders();
  h.api.keyword.value='';h.api.search();await settle(()=>!h.api.loading.value);
  assert.equal(h.api.page.value,1);assert.equal(h.api.lastSuccessfulQuery.value.keyword,'');assert.match(source,/@clear="search"/);
  h.api.keyword.value='NEW';h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'',scope:'active'}]);
  h.api.scope.value='completed';h.api.search();await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',1,20,{keyword:'NEW',scope:'completed'}]);
  h.api.keyword.value='unsent';h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'NEW',scope:'completed'}]);
});
test('KeepAlive retains unsubmitted query and detail edits; paging still uses committed query and fixed pageSize20',async()=>{
  const h=harness();attachTable(h);h.api.keyword.value='OLD';await h.api.loadOrders();await h.api.openDetail(1);
  h.api.keyword.value='NEW';h.api.editing.value=true;h.api.editForm.value.remark='SYNTHETIC unsaved edit';
  const form=h.api.editForm.value,rows=h.api.rows.value;h.hooks.deactivated[0]();h.hooks.activated[0]();await Promise.resolve();h.flush();
  assert.equal(h.api.editForm.value,form);assert.equal(h.api.editForm.value.remark,'SYNTHETIC unsaved edit');assert.equal(h.api.editing.value,true);
  assert.equal(h.api.rows.value,rows);assert.equal(h.api.keyword.value,'NEW');assert.equal(h.api.lastSuccessfulQuery.value.keyword,'OLD');
  h.api.changePage(2);await settle(()=>!h.api.loading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.at(-1))),['list',2,20,{keyword:'OLD',scope:'active'}]);
  assert.equal(h.api.pageSize,20);assert.match(source,/:page-size="pageSize"/);assert.doesNotMatch(source,/@size-change|v-model:page-size|page-sizes=/);
  assert.equal(h.api.editForm.value,form);assert.equal(h.api.editForm.value.remark,'SYNTHETIC unsaved edit');h.hooks.unmounted[0]();
});

test('saved order target refreshes cached committed list without submitting draft controls',async()=>{
  const h=harness();h.api.keyword.value='OLD';await h.api.loadOrders();
  h.api.keyword.value='UNSUBMITTED';h.api.scope.value='completed';
  h.impl.list=async()=>({items:[{...fixture,id:2}],total:2});
  h.hooks.watch[0].callback('2');await settle(()=>!h.api.loading.value && !h.api.detailLoading.value);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.filter(row=>row[0]==='list').at(-1))),['list',1,20,{keyword:'OLD',scope:'active'}]);
  assert.equal(h.api.keyword.value,'UNSUBMITTED');assert.equal(h.api.scope.value,'completed');assert.equal(h.api.rows.value[0].id,2);
  const count=h.calls.length;h.route.path='/requisition';h.hooks.watch[0].callback('3');assert.equal(h.calls.length,count);
});

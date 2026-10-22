const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ts=require('../../frontend-v2/node_modules/typescript');
const base=path.resolve(__dirname,'../../frontend-v2/src/mobile');
const tick=()=>new Promise(r=>setImmediate(r));
function deferred(){let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j});return {promise,resolve,reject};}
function evaluate(source,modules){const module={exports:{}};const context={module,exports:module.exports,require:n=>modules[n]||{},URL,URLSearchParams,AbortController,console,window:{location:{origin:'http://localhost'}},document:{title:''}};vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,context);return module.exports;}
const navigation=evaluate(fs.readFileSync(path.join(base,'navigation.ts'),'utf8'),{});
function setup(file,{route={path:'/mobile/query',fullPath:'/mobile/query',query:{},params:{}},api={},shell={entries:[]},names=[]}={}){
 const watch=[],unmount=[],replaces=[];const auth={user:{id:1,role:'admin'}},mobile={shell,allowed:()=>true,entry:()=>({})};
 const vue={ref:value=>({value}),computed:get=>({get value(){return get();}}),watch:(source,fn,options)=>{watch.push({source,fn});if(options?.immediate)void fn();},onMounted:()=>{},onBeforeUnmount:fn=>unmount.push(fn)};
 const format={safeMessage:e=>String(e.message||e),compactText:x=>String(x||''),quantity:x=>String(x),shortLocation:x=>String(x||'')};
 const code=fs.readFileSync(path.join(base,file),'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]+`\nmodule.exports={${names.join(',')}};`;
 const result=evaluate(code,{vue,'vue-router':{useRoute:()=>route,useRouter:()=>({replace:x=>{replaces.push(x);return Promise.resolve();},push:()=>{}})},'./api':{mobileApi:api,legacyUrl:()=>'',backendUrl:x=>x},'./navigation':navigation,'./store':{useMobileStore:()=>mobile},'../stores/auth':{useAuthStore:()=>auth},'./format':format});
 return {...result,watch,unmount,replaces,mobile,auth,route};
}
test('detail returns to exact safe query/page; tokens and arbitrary destinations never survive',()=>{
 assert.equal(navigation.mobileParent('/mobile/products/12','/mobile/query?tab=location&q=A&page=3&token=secret'),'/mobile/query?tab=location&q=A&page=3');
 for(const s of ['https://evil.test','//evil.test','/mobile/../login','/mobile/query#bad','/mobile/query\\evil']) assert.equal(navigation.mobileReturn(s),null);
 assert.equal(navigation.mobileSection('/mobile/products/12','/mobile/warehouse?floor=2'),'/mobile/warehouse');
 assert.equal(navigation.mobileSection('/mobile/products/12',null),'/mobile/query');
});
test('unfulfilled home count arrives without waiting for summary; old account result is ignored',async()=>{
 const slow=deferred(),fast=deferred();let unfulfilledCalls=0;
 const h=setup('MobileHome.vue',{api:{overview:()=>slow.promise,unfulfilled:()=>{unfulfilledCalls++;return fast.promise;}},shell:{management_summary_allowed:true,search_categories:['orders'],delivery_margin_allowed:false},names:['load','pendingOrders','overview','loading']});
 const loading=h.load();assert.equal(unfulfilledCalls,1);fast.resolve({groups:[{total:8}]});await tick();assert.equal(h.pendingOrders.value,8);
 h.mobile.shell={entries:[],search_categories:[],management_summary_allowed:false};await h.load();slow.resolve({month:'old'});await loading;
 assert.equal(h.overview.value,null);assert.equal(h.pendingOrders.value,undefined);assert.equal(h.loading.value,false);
});
test('location page 2 is requested, remembered and available to detail return',async()=>{
 const calls=[];const h=setup('MobileQuery.vue',{route:{path:'/mobile/query',fullPath:'/mobile/query?tab=location&q=A&page=2',query:{tab:'location',q:'A',page:'2'},params:{}},api:{locations:async(...x)=>{calls.push(x);return {items:[{location_id:19}],total:30};}},names:['locations','page','total']});
 await tick();assert.equal(calls[0][2],2);assert.equal(h.page.value,2);assert.equal(h.locations.value[0].location_id,19);assert.equal(h.replaces[0].query.tab,'location');assert.equal(h.replaces[0].query.page,'2');
});
test('changing search tab releases loading and late old result cannot refill it',async()=>{
 const late=deferred();const h=setup('MobileQuery.vue',{api:{products:()=>late.promise},names:['search','query','tab','products','loading','total']});
 h.query.value='A';const reading=h.search();assert.equal(h.loading.value,true);h.tab.value='location';h.watch[0].fn();
 assert.equal(h.loading.value,false);late.resolve({items:[{id:9}],total:30});await reading;assert.equal(h.products.value.length,0);assert.equal(h.total.value,0);
});
test('account switch clears query results while read pending',async()=>{
 const late=deferred();const h=setup('MobileQuery.vue',{api:{products:()=>late.promise},names:['search','query','products','loading']});
 h.query.value='old';const reading=h.search();h.auth.user={id:2};h.watch[1].fn();late.resolve({items:[{id:99}],total:1});await reading;
 assert.equal(h.products.value.length,0);assert.equal(h.loading.value,false);
});
test('query return restores dimension mode and requested page instead of keyword first page',async()=>{
 const calls=[];setup('MobileQuery.vue',{route:{path:'/mobile/query',fullPath:'/mobile/query',query:{mode:'dimensions',length:'300',width:'200',page:'3',tolerance:'50'},params:{}},api:{products:async p=>{calls.push(p);return {items:[],total:0};}}});
 await tick();assert.equal(calls[0].get('dimension_basis'),'finished');assert.equal(calls[0].get('page'),'3');assert.equal(calls[0].get('tolerance_mm'),'50');
});
test('invalid page cannot produce an unbounded request',()=>{for(const p of ['NaN','Infinity','0','-2','2.5','100001'])assert.equal(navigation.mobilePage(p),1);});

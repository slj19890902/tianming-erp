const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {parseHTML}=require(require.resolve('linkedom',{paths:[__dirname+'/ui',__dirname]}));
const source=fs.readFileSync('static/ui/mobile-shell.js','utf8');
function env({body='',search='',parentOrigin,fetch=async()=>({ok:false,status:401})}={}){
 const dom=parseHTML(`<html><body ${body}><main id="business">existing workflow</main></body></html>`),document=dom.document;
 Object.defineProperty(document,'readyState',{value:'loading'});
 const events=new Map();const win={document,location:{origin:'https://erp.example',search},fetch,addEventListener(type,handler){if(!events.has(type))events.set(type,[]);events.get(type).push(handler)},dispatchEvent(event){for(const fn of events.get(event.type)||[])fn(event);return !event.defaultPrevented}};
 win.parent=parentOrigin?{location:{origin:parentOrigin}}:win;
 const context={window:win,URL,URLSearchParams,AbortController,CustomEvent:dom.CustomEvent};
 vm.runInNewContext(source,context);return {win,document,dom,shell:win.TmMobileShell};
}
const full={user:{id:7},entries:[{id:'lookup'},{id:'warehouse'},{id:'incoming'},{id:'production'},{id:'pre_delivery'}]};
test('shared navigation is permission derived and returns to the actual SPA',()=>{
 const e=env({body:'data-mobile-page="warehouse"'});e.shell.mount({shell:full});
 assert.equal(e.document.querySelectorAll('.tm-shell-nav a').length,4);
 assert.equal(e.document.querySelector('[aria-current]').getAttribute('href'),'/frontend-v2/mobile/warehouse');
 e.shell.update({user:{id:9},entries:[{id:'incoming'}]});
 assert.deepEqual([...e.document.querySelectorAll('.tm-shell-nav a')].map(a=>a.textContent),['首页','更多']);
 assert.equal(e.document.querySelector('#business').textContent,'existing workflow');
});
test('only actual same-origin mobile embeds omit common navigation',()=>{
 const embedded=env({body:'data-mobile-page="more"',search:'?embedded=1',parentOrigin:'https://erp.example'});embedded.shell.mount({shell:full});
 assert.equal(embedded.document.querySelector('.tm-shell-nav'),null);
 const direct=env({body:'data-mobile-page="more"',search:'?embedded=1'});direct.shell.mount({shell:full});assert.equal(direct.document.querySelectorAll('.tm-shell-nav a').length,4);
 const foreign=env({body:'data-mobile-page="more"',search:'?embedded=1',parentOrigin:'https://other.example'});foreign.shell.mount({shell:full});assert.equal(foreign.document.querySelectorAll('.tm-shell-nav a').length,4);
});
test('desktop incoming is unchanged and token authorization is never replaced by shell login',async()=>{
 const desktop=env({body:'data-mobile-embedded-only="1"'});assert.equal(desktop.shell.mount(),null);assert.equal(desktop.document.body.className,'');
 let count=0;const token=env({search:'?token=private-token',fetch:async()=>{count++;return{ok:false,status:401}}});token.shell.mount();await new Promise(r=>setImmediate(r));assert.equal(count,1);assert.equal(token.document.querySelector('#business').textContent,'existing workflow');assert.equal(token.document.querySelector('.tm-shell-nav').hidden,true);
});
test('return addresses reject external/action/credential URLs and preserve exact safe object context',()=>{
 const {shell}=env();assert.equal(shell.safeReturn('/mobile/?warehouse_map=1&location_id=23#warehouse'),'/mobile/?warehouse_map=1&location_id=23#warehouse');
 for(const value of ['https://other.example/mobile/','/api/warehouse/delete','javascript:alert(1)','https://user:pass@erp.example/mobile/','/mobile/tianhua-pick?token=secret'])assert.equal(shell.safeReturn(value),null,value);
});
test('navigation retains the owning executors busy guard and existing return control',()=>{
 const e=env({body:'data-mobile-return-control="originalReturn"'});const b=e.document.createElement('button');b.id='originalReturn';e.document.body.append(b);let original=0;b.addEventListener('click',()=>original++);e.shell.mount({shell:full});
 const click=new e.dom.Event('click',{bubbles:true,cancelable:true});e.document.querySelector('.tm-shell-back').dispatchEvent(click);assert.equal(original,1);assert.equal(click.defaultPrevented,true);b.disabled=true;const disabled=new e.dom.Event('click',{bubbles:true,cancelable:true});e.document.querySelector('.tm-shell-back').dispatchEvent(disabled);assert.equal(disabled.defaultPrevented,true);assert.equal(original,1);
 e.win.addEventListener('tm-mobile-shell-navigate',event=>event.preventDefault());const blocked=new e.dom.Event('click',{bubbles:true,cancelable:true});e.document.querySelector('.tm-shell-nav a').dispatchEvent(blocked);assert.equal(blocked.defaultPrevented,true);
});
test('read epochs abort old reads and never regard cancelled responses as current',()=>{
 const {shell}=env(),reads=shell.readEpoch(),first=reads.begin(),second=reads.begin();assert.equal(first.signal.aborted,true);assert.equal(first.current(),false);assert.equal(second.current(),true);reads.cancel();assert.equal(second.current(),false);
});
function liveEnv(file){
 const html=fs.readFileSync('static/'+file,'utf8');const load=html.slice(html.indexOf('    async function load(){'),html.indexOf('    $("refresh").onclick=load;')).replace(/    window.addEventListener[^\n]+\n/g,'');
 const {document}=parseHTML('<html><body><div id="message"></div><div id="content"></div><button id="refresh"></button></body></html>');
 const pending=[];const context={document,AbortController,Date,window:{TmProductDrawings:{disposeWithin(){}}},$:(id)=>document.getElementById(id),api:(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject})),render:data=>{document.getElementById('content').textContent=data.label},liveDataUrl:()=>'/mold/live',recordScan:async()=>{throw Error('unexpected write')}};
 vm.createContext(context);vm.runInContext('let loadGeneration=0,loadController=null,scanPending=false,latestData=null,scanEvent=null,selectedTaskId=null;const moldId=1,productId=1;'+load+'\nthis.load=load;',context);
 return{context,document,pending};
}
test('product late read cannot replace the latest product and failed refresh is visible',async()=>{
 const e=liveEnv('mobile_product_live.html'),first=e.context.load(),second=e.context.load();e.pending[1].resolve({label:'latest'});await second;e.pending[0].resolve({label:'old'});await first;assert.equal(e.document.getElementById('content').textContent,'latest');
 const failure=e.context.load();e.pending[2].reject(Error('offline'));await failure;assert.match(e.document.getElementById('message').textContent,/上次读取/);assert.equal(e.document.getElementById('content').classList.contains('tm-stale-data'),true);
 const denied=e.context.load();e.pending[3].reject(Object.assign(Error('denied'),{status:403}));await denied;assert.equal(e.document.getElementById('content').textContent,'');
});
test('mold read ignores late identity checks and never interrupts a pending scan write',async()=>{
 const e=liveEnv('mobile_mold_live.html'),first=e.context.load(),second=e.context.load();e.pending[1].resolve({permissions:['warehouse.view']});await new Promise(r=>setImmediate(r));e.pending[2].resolve({label:'latest',current_orders:{items:[]}});await second;e.pending[0].resolve({permissions:['warehouse.view']});await first;assert.equal(e.document.getElementById('content').textContent,'latest');assert.equal(e.pending.length,3);
 vm.runInContext('scanPending=true',e.context);await e.context.load();assert.equal(e.pending.length,3);
});
function queryEnv(){
 const html=fs.readFileSync('static/mobile_erp.html','utf8'),source=html.slice(html.indexOf('      async function runSearch(options'),html.indexOf('      async function loadInventory'));
 const {document}=parseHTML('<html><body><input id="searchInput" value="first"><input id="includeZeroStock"><div id="candidateList"></div><div id="productResult"></div><div id="searchState"></div></body></html>');
 const pending=[],state={activePage:'warehouse',shell:{user:{id:7,role:'admin'}},searchGeneration:0,detailGeneration:0,candidates:[],searchLoading:false};
 const context={state,document,AbortController,window:{TmProductDrawings:{disposeWithin(){},append(){}}},byId:id=>document.getElementById(id),node:(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls;if(text)n.textContent=text;return n},warehouseActor:()=>7,fieldIntent(){},queueSearch(){},showStatus(id,text,cls,retry){const n=document.getElementById(id);n.textContent=text;n.hidden=false;n.retry=retry;},apiGet:(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject})),loadInventory(){}};
 vm.createContext(context);vm.runInContext(source+'\nthis.search=runSearch;',context);return {context,state,document,pending};
}
const product=id=>({id,product_code:'DEMO-'+id,customer_name:'Fixture',product_name:'Box',inventory_summary:{has_stock:true}});
test('warehouse candidate pages append without duplicates; double-click and stale query responses cannot corrupt them',async()=>{
 const e=queryEnv(),first=e.context.search();assert.match(e.pending[0].url,/offset=0/);e.pending[0].resolve({items:[product(1),product(2)],total:3,has_more:true,next_offset:2});await first;
 const append=e.context.search({append:true});await e.context.search({append:true});assert.equal(e.pending.length,2);assert.match(e.pending[1].url,/offset=2/);e.pending[1].resolve({items:[product(2),product(3)],total:3,has_more:false,next_offset:null});await append;assert.equal(e.state.candidates.map(x=>x.id).join(','),'1,2,3');
 const old=e.context.search();e.document.getElementById('searchInput').value='new';const fresh=e.context.search();assert.equal(e.state.candidates.length,0);assert.equal(e.pending[2].options.signal.aborted,true);e.pending[3].resolve({items:[product(4)],total:1,has_more:false});await fresh;e.pending[2].resolve({items:[product(1)],total:1});await old;assert.equal(e.state.candidates[0].id,4);
});
test('first nearby-dimension failure is visible and offers an explicit read retry',async()=>{
 const html=fs.readFileSync('static/mobile_erp.html','utf8');const source=html.slice(html.indexOf('      async function runFieldNearby('),html.indexOf('      function clearLookup()'));
 const {document}=parseHTML('<html><body><div id="fieldNearby"></div><details id="fieldNearbyDetails" hidden></details><div id="fieldNearbyPager"></div><input id="fieldBoardState" value="raw"><input id="fieldTolerance" value="5"><input id="fieldMaterial"><input id="fieldFlute"></body></html>');
 const context={document,URLSearchParams,state:{shell:{user:{id:7}},fieldNearbyGeneration:0,fieldIntent:'board',activePage:'lookup'},byId:id=>document.getElementById(id),node:(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n},apiGet:async()=>{throw Error('offline')}};vm.createContext(context);vm.runInContext(source+'this.run=runFieldNearby',context);await context.run({length:900,width:600});assert.equal(document.getElementById('fieldNearbyDetails').hidden,false);assert.match(document.getElementById('fieldNearby').textContent,/offline/);assert.equal(document.querySelectorAll('#fieldNearby button').length,1);
});
test('returning to a multi-page picking list reloads the full browsed window',async()=>{
 const html=fs.readFileSync('static/mobile_delivery_pick.html','utf8');const source=html.slice(html.indexOf('    async function loadTaskList('),html.indexOf('    async function selectTask(id)'));
 const {document}=parseHTML('<html><body><div id="taskChooser"></div><div id="error"></div></body></html>'),calls=[],renderLoading=[];let failNext=false;
 const context={document,AbortController,renderTaskChooser(){renderLoading.push(context.result().listLoading)},handleAuthError(){return false},toast(){},escapeHtml:String,request:async url=>{calls.push(url);if(failNext)throw Error('offline');const page=Number(new URL(url,'https://erp.example').searchParams.get('page'));return{items:Array.from({length:20},(_,i)=>({id:(page-1)*20+i+1})),page,total:80,as_of:'fixture'}}};
 vm.createContext(context);vm.runInContext('let listGeneration=0,listController=null,listLoading=false,listRows=[],listPage=0,listTotal=0,listHasMore=false,listAsOf="";const listPageSize=20;'+source+'this.load=loadTaskList;this.result=()=>({listPage,listLoading,ids:listRows.map(x=>x.id)});',context);assert.equal(await context.load({restorePages:3}),true);assert.equal(calls.length,3);assert.equal(context.result().listPage,3);assert.equal(context.result().ids.length,60);
 failNext=true;assert.equal(await context.load({restorePages:3}),false);assert.equal(context.result().ids.length,60);assert.equal(renderLoading.at(-1),false);assert.match(document.getElementById('error').textContent,/列表尚未刷新/);assert.equal(document.querySelectorAll('#error button').length,1);
});

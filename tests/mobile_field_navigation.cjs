const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),{test}=require('node:test');
const root=path.resolve(__dirname,'..'),moduleSource=fs.readFileSync(path.join(root,'static/ui/mobile-field-return.js'),'utf8'),html=fs.readFileSync(path.join(root,'static/mobile_erp.html'),'utf8');
const clone=x=>JSON.parse(JSON.stringify(x));
function storage(map=new Map()){return {map,get length(){return map.size},key:i=>[...map.keys()][i]??null,getItem:k=>map.get(k)??null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)}}
function fieldModule(){const window={};vm.runInNewContext(moduleSource,{window,URLSearchParams,Uint8Array});return window.TmMobileFieldReturn;}
const values={kind:'search',query:'长编码',includeZero:true,selectedProductId:5,locationId:42,floorCode:'3F',areaCode:'A',scrollY:210};
test('navigation records are independent, owner-bound, limited, expiring, and contain no inventory data',()=>{
 const m=fieldModule(),s=storage(),now=Date.now(),first=m.capture(s,101,{...values,quantity_available:88,inventory:{quantity:99}},now);
 const second=m.capture(s,101,{...values,query:'另一次查询'},now+1);
 assert.notEqual(first,second);assert.equal(m.read(s,101,first,now+2).query,'长编码');assert.equal(m.read(s,202,first,now+2),null);
 assert.equal(m.read(s,101,first,now+m.lifetime+1),null);assert.ok(!JSON.stringify(m.read(s,101,first,now+2)).includes('quantity'));
 s.setItem('tm-mobile-stocktake-recovery-v1:101:business','do not touch');const other=m.capture(s,202,values,now);
 for(let n=0;n<25;n++)m.capture(s,101,values,now+n+10);
 assert.equal([...s.map.keys()].filter(k=>k.startsWith(m.prefix+'101:')).length,20);
 assert.ok(m.read(s,202,other,now+40));assert.equal(s.getItem('tm-mobile-stocktake-recovery-v1:101:business'),'do not touch');
});
test('blocked/malformed convenience storage degrades; scan targets reject arbitrary URLs and malformed tokens',()=>{
 const m=fieldModule(),s=storage();s.getItem=()=>{throw Error('blocked')};assert.equal(m.read(s,101,'a'.repeat(32)),null);assert.equal(m.capture(s,101,values),null);
 assert.equal(m.scanTarget('42','abcdef012345abcdef012345'),'/q/42/abcdef012345abcdef012345');
 for(const [id,token] of [['//evil',''],['0',''],['9007199254740992',''],['42','../other'],['42','ABCDEF012345abcdef012345']])assert.equal(m.scanTarget(id,token),null);
 const search=m.target(new URLSearchParams('return_source=search&return_context=../../evil&return_url=https://evil'));assert.equal(search.href,'/mobile/?return_source=search#warehouse');
 assert.equal(m.target(new URLSearchParams('return_source=map')),null);
});
class Element{
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.options=[];this.value='';this.checked=false;this.hidden=false;this.disabled=false;this.dataset={};this.attrs={};this.className='';this.textContent='';this.style={setProperty(){}};this.classList={toggle(){},add(){},remove(){}};}
 append(...nodes){this.children.push(...nodes)}replaceChildren(...nodes){this.children=nodes;this.textContent=''}addEventListener(k,fn){this.listeners[k]=fn}removeEventListener(){}setAttribute(k,v){this.attrs[k]=v}getAttribute(k){return this.attrs[k]}scrollIntoView(){}querySelector(){return new Element()}querySelectorAll(){return []}getBoundingClientRect(){return {width:360,height:600}}focus(){}
}
function mobileHarness({session=storage(),search='',actor=101,role='admin'}={}){
 const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)};
 for(const match of html.matchAll(/id="([^"]+)"/g))get(match[1]);get('warehouseMapLayer').hidden=true;
 const navs=['home','lookup','warehouse','incoming','production','pre_delivery','more'].map(page=>{const e=new Element('button');e.dataset.page=page;return e});
 const qs=selector=>selector.startsWith('[data-page=')?navs.find(e=>e.dataset.page===selector.match(/"(.*?)"/)[1]):get(selector);
 const events={},data={actor,role,calls:[],assigned:[],detailMode:'normal',deferDetail:null,deferSearch:null,deferShell:null,quantity:20},local=storage();
 const window={sessionStorage:session,localStorage:local,location:{search,pathname:'/mobile/',hash:'#warehouse',assign:url=>data.assigned.push(url),replace:url=>data.assigned.push(url)},history:{replaceState(_a,_b,url){if(url.startsWith('#'))window.location.hash=url}},scrollY:210,scrollTo(){},requestAnimationFrame:fn=>fn(),setTimeout,clearTimeout,addEventListener:(k,fn)=>events[k]=fn,removeEventListener(){},innerHeight:800};
 const document={getElementById:get,querySelector:qs,querySelectorAll:s=>s==='.nav-btn'?navs:[],body:new Element(),createElement:t=>new Element(t),createElementNS:(_ns,t)=>new Element(t),addEventListener(){},visibilityState:'visible'};
 const shell=()=>({user:{id:data.actor,role:data.role,display_name:'合成用户'},entries:[{id:'lookup',title:'综合查询'},...(data.noWarehouse?[]:[{id:'warehouse',title:'查货',can_execute:true,can_correct:true}])],search_categories:data.orders?['orders']:[]});
 const product={id:5,product_code:'长编码',product_name:'合成纸箱',customer_name:'隔离甲',inventory_summary:{has_stock:true}};
 const response=(body,status=200)=>({status,ok:status===200,text:async()=>JSON.stringify(body)});
 const context=vm.createContext({window,document,sessionStorage:session,localStorage:local,URLSearchParams,AbortController,CSS:{escape:x=>String(x)},setTimeout,clearTimeout,console,fetch:async url=>{
  data.calls.push(url);if(url.endsWith('/shell')){if(data.deferShell)await data.deferShell;if(data.shellFail)throw Error('合成账号读取失败');return response(shell())}
  if(url.endsWith('/dimension-settings')){if(data.deferDimension)await data.deferDimension;return response({near_mm:5,expanded_mm:10,version:1,can_edit:false})}
  if(url.includes('/products?')){const status=data.searchStatus||200;if(data.deferSearch)await data.deferSearch;return response({items:data.omitProduct?[]:[product],requires_selection:false},status)}
  if(url.endsWith('/inventory')){const status=data.detailStatus||200;if(data.deferDetail)await data.deferDetail;if(data.detailMode==='fail')throw Error('合成明细失败');return response({product,inventory:{finished:{quantity_total:data.quantity,quantity_available:data.quantity,quantity_reserved:0,positions:[]},semi_finished:{positions:[]}}},status)}
  if(url.endsWith('/map/floors'))return response({floors:[{floor_code:'3F',areas:[{area_code:'A',map_status:'ready'}]}]});
  if(url.includes('/map/floors/'))return response({floor_code:'3F',locations:[],bounds:{min_x:0,min_y:0,max_x:10,max_y:10},features:[]});
  throw Error('Unexpected synthetic read '+url);
 }});
 vm.runInContext(moduleSource,context);
 const inline=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1].replace('      initialize();','      window.__field={state,initialize,runSearch,loadInventory,queueSearch,restoreWarehouseSearch,refreshWarehouseAfterBack,launchMobileStocktake,openWarehouseMap,openMap,setPage};');
 vm.runInContext(inline,context);
 return {window,context,data,get,page:window.__field,events,session,run:s=>vm.runInContext(s,context)};
}
test('actual mobile initialize restores owner keyword/product from new GETs; non-admin include-zero is false',async()=>{
 const s=storage(),m=fieldModule(),token=m.capture(s,101,values),h=mobileHarness({session:s,search:'?return_source=search&return_context='+token,role:'warehouse'});
 await h.page.initialize();assert.equal(h.get('searchInput').value,'长编码');assert.equal(h.page.state.selectedProductId,5);assert.equal(h.page.state.selectedInventory.inventory.finished.quantity_total,20);
 assert.ok(h.data.calls.some(url=>url.includes('include_zero=false')));assert.equal(h.get('includeZeroStock').checked,false);
 assert.equal(h.data.calls.filter(url=>url.endsWith('/inventory')).length,1);
 await h.events.pageshow({persisted:false});assert.equal(h.data.calls.filter(url=>url.endsWith('/inventory')).length,1);
});
for(const reason of ['other-account','missing','expired','storage-error'])test(`actual return ${reason} retains no stale product or quantity`,async()=>{
 const s=storage(),m=fieldModule(),token=m.capture(s,101,values,reason==='expired'?Date.now()-m.lifetime-10:Date.now());if(reason==='storage-error')s.getItem=()=>{throw Error('blocked')};
 const h=mobileHarness({session:s,actor:reason==='other-account'?202:101,search:'?return_source=search&return_context='+(reason==='missing'?'a'.repeat(32):token)});await h.page.initialize();assert.equal(h.page.state.selectedInventory,null);assert.equal(h.get('searchInput').value,'');assert.match(h.get('searchState').children[0].textContent,/重新查询/);
});
test('selected product GET failure retains keyword but never renders cached quantities',async()=>{
 const h=mobileHarness();await h.page.initialize();h.data.detailMode='fail';await h.page.restoreWarehouseSearch(values);assert.equal(h.get('searchInput').value,'长编码');assert.equal(h.page.state.selectedInventory,null);assert.equal(h.get('productResult').hidden,true);
});
test('saved product beyond current search limit still uses authoritative inventory endpoint',async()=>{
 const h=mobileHarness();await h.page.initialize();h.data.omitProduct=true;await h.page.restoreWarehouseSearch(values);assert.equal(h.page.state.selectedProductId,5);assert.ok(h.data.calls.some(url=>url.endsWith('/products/5/inventory')));assert.equal(h.page.state.selectedInventory.inventory.finished.quantity_total,20);
});
test('warehouse permission loss on persisted map return closes overlay and removes all old map facts',async()=>{
 const h=mobileHarness();await h.page.initialize();h.get('warehouseMapLayer').hidden=false;h.get('warehouseRackList').append(new Element());h.get('warehouseFloorOverview').append(new Element());h.data.noWarehouse=true;await h.events.pageshow({persisted:true});assert.equal(h.get('warehouseMapLayer').hidden,true,JSON.stringify({page:h.page.state.activePage,home:h.get('homeState').children.map(e=>e.textContent),calls:h.data.calls}));assert.equal(h.get('warehouseRackList').children.length,0);assert.equal(h.get('warehouseFloorOverview').children.length,0);assert.notEqual(h.page.state.activePage,'warehouse');
});
test('map identity failure remains retryable from main search after closing overlay',async()=>{
 const h=mobileHarness();await h.page.initialize();h.get('warehouseMapLayer').hidden=false;h.data.shellFail=true;await h.events.pageshow({persisted:true});assert.equal(h.page.state.warehouseAuthPending,true);assert.ok(h.get('searchState').children.some(e=>e.tagName==='BUTTON'));h.get('closeWarehouseMap').listeners.click();assert.equal(h.get('warehouseMapLayer').hidden,true);h.data.shellFail=false;const retry=h.get('searchState').children.find(e=>e.tagName==='BUTTON');await retry.listeners.click();assert.equal(h.page.state.warehouseAuthPending,false);h.get('searchInput').value='新查货';await h.page.runSearch();assert.ok(h.page.state.candidates.length);
});
for(const operation of ['search','detail'])for(const status of [200,401])test(`ignored abort: late ${operation} ${status} cannot redirect or replace newer request`,async()=>{
 const h=mobileHarness();await h.page.initialize();let release;const slow=new Promise(r=>release=r);
 h.get('searchInput').value='旧查询';h.data[operation==='search'?'deferSearch':'deferDetail']=slow;h.data[operation==='search'?'searchStatus':'detailStatus']=status;
 const old=operation==='search'?h.page.runSearch():h.page.loadInventory(5);h.data[operation==='search'?'deferSearch':'deferDetail']=null;h.data[operation==='search'?'searchStatus':'detailStatus']=200;
 h.get('searchInput').value='新查询';await h.page.runSearch();await h.page.loadInventory(6);release();await old;assert.equal(h.page.state.selectedProductId,6);assert.equal(h.get('searchInput').value,'新查询');assert.equal(h.data.assigned.length,0);
});
test('persisted return rechecks identity and reloads current inventory; ordinary initial show makes no reads',async()=>{
 const h=mobileHarness();await h.page.initialize();h.get('searchInput').value='长编码';await h.page.runSearch();await h.page.loadInventory(5);h.data.quantity=31;const before=h.data.calls.length;await h.events.pageshow({persisted:true});assert.equal(h.page.state.selectedInventory.inventory.finished.quantity_total,31);assert.equal(h.data.calls[before],'/api/mobile/erp/shell');
 h.data.actor=202;await h.events.pageshow({persisted:true});assert.equal(h.page.state.selectedInventory,null);assert.equal(h.get('searchInput').value,'');assert.match(h.get('searchState').children[0].textContent,/账号已变化/);
});
for(const action of ['typing','product','page'])test(`late restore cannot overwrite active ${action}`,async()=>{
 const h=mobileHarness();await h.page.initialize();let release;h.data.deferSearch=new Promise(r=>release=r);const old=h.page.restoreWarehouseSearch(values);
 if(action==='typing'){h.get('searchInput').value='新编码';h.page.queueSearch();clearTimeout(h.page.state.searchTimer)}
 if(action==='product'){h.data.deferSearch=null;await h.page.loadInventory(6)}
 if(action==='page')h.page.setPage('lookup');release();await old;
 if(action==='typing')assert.equal(h.get('searchInput').value,'新编码');if(action==='product')assert.equal(h.page.state.selectedProductId,6);if(action==='page')assert.equal(h.page.state.activePage,'lookup');assert.equal(h.page.state.selectedProductId===5,false);
});
test('typing during slow identity read is retained and queried with fresh same-account permissions',async()=>{
 const h=mobileHarness();await h.page.initialize();h.get('searchInput').value='旧关键词';let release;h.data.deferShell=new Promise(r=>release=r);const returning=h.events.pageshow({persisted:true});h.get('searchInput').value='新关键词';h.page.queueSearch();release();await returning;assert.equal(h.get('searchInput').value,'新关键词');assert.ok(h.data.calls.some(url=>url.includes(encodeURIComponent('新关键词'))));
});
test('persisted navigation never replaces an unfinished map business key or source',async()=>{
 const h=mobileHarness();await h.page.initialize();const source={body:'original-move'};h.page.state.warehouseMapSource=source;h.page.state.warehouseMapMoveRequestKey='same-move-key';const count=h.data.calls.length;await h.events.pageshow({persisted:true});assert.equal(h.data.calls.length,count);assert.equal(h.page.state.warehouseMapSource,source);assert.equal(h.page.state.warehouseMapMoveRequestKey,'same-move-key');
});
test('active page change during fresh-shell initialization is preserved after dependent GET returns',async()=>{
 const h=mobileHarness();await h.page.initialize();h.data.orders=true;let release;h.data.deferDimension=new Promise(r=>release=r);const returning=h.events.pageshow({persisted:true});for(let n=0;n<5&&!h.data.calls.some(url=>url.endsWith('/dimension-settings'));n++)await new Promise(r=>setImmediate(r));assert.ok(h.data.calls.some(url=>url.endsWith('/dimension-settings')));h.page.setPage('lookup');release();await returning;assert.equal(h.page.state.activePage,'lookup');assert.equal(h.window.location.hash,'#lookup');assert.equal(h.data.calls.filter(url=>url.includes('/products?')).length,0);
});
test('launch hook distinguishes explicit map source and preserves initial-add purpose fields',async()=>{
 const h=mobileHarness();await h.page.initialize();h.get('searchInput').value='长编码';h.page.state.selectedProductId=5;h.page.state.warehouseMapOrigin='map';
 const adding=new URLSearchParams({location_id:42,product_id:5,customer_id:1,product_code:'长编码',suggested_quantity:7});h.page.launchMobileStocktake(adding);let url=new URL(h.data.assigned.at(-1),'http://synthetic');assert.equal(url.searchParams.get('return_source'),'map');assert.equal(url.searchParams.get('product_id'),'5');assert.equal(url.searchParams.get('suggested_quantity'),'7');
 h.page.state.warehouseMapOrigin='search';h.page.launchMobileStocktake(new URLSearchParams({location_id:42}));url=new URL(h.data.assigned.at(-1),'http://synthetic');assert.equal(url.searchParams.get('return_source'),'search');assert.equal(h.window.TmMobileFieldReturn.read(h.session,101,url.searchParams.get('return_context')).query,'长编码');
 assert.equal((html.match(/launchMobileStocktake\(params\);/g)||[]).length,4);
});
// Reuse the existing full actual stocktake page harness, not a duplicate implementation.
const existing=fs.readFileSync(path.join(__dirname,'mobile_stocktake_recovery.cjs'),'utf8');
const stocktakeHarness=new Function('require','__dirname',existing.slice(0,existing.indexOf('const cases=[]'))+'\nreturn harness;')(require,__dirname);
test('durable unknown can leave and reenter while exact key/body/count remain frozen; batch relocation bypasses source',async()=>{
 const h=stocktakeHarness();h.run(moduleSource);h.window.location.search='?location_id=42&return_source=search&return_context='+'a'.repeat(32);h.data.mode='network-before';await h.submit();const [key,raw]=[...h.data.storage][0];
 assert.equal(h.run('openWarehouseMap()'),true);assert.match(h.data.redirects.at(-1),/^\/mobile\/\?return_source=search/);assert.equal(h.data.storage.get(key),raw);
 const again=stocktakeHarness({storage:h.data.storage});assert.equal(again.get('count-7').value,'20');assert.equal(again.get('count-7').disabled,true);await again.submit();assert.equal(again.writes().length,0);assert.equal(again.data.storage.get(key),raw);
 h.run('openWarehouseMap(7)');assert.match(h.data.redirects.at(-1),/lot_id=7/);assert.doesNotMatch(h.data.redirects.at(-1),/return_source/);
});
for(const issue of ['busy','accountChanged','issue','inbound'])test(`navigation stays conservative for ${issue}`,async()=>{
 const h=stocktakeHarness();if(issue==='inbound')h.run("const inbound={busy:false,attempt:{idempotency_key:'unknown'}}");else h.run(`stocktakeRecoveryState.${issue}=${issue==='issue'?"'storage failed'":'true'}`);h.run('openWarehouseMap()');assert.equal(h.data.redirects.length,0);
});
test('actual scanner preserves validated token and stocktake visible labels match source; submit precedes add form',()=>{
 const scan=fs.readFileSync(path.join(root,'static/shelf-scan.js'),'utf8'),page=fs.readFileSync(path.join(root,'static/mobile_stocktake.html'),'utf8');assert.match(scan,/returnParams\.set\('return_product',product\)/);assert.match(scan,/\[a-f0-9\]\{24\}/);assert.ok(page.indexOf('id="submitArea"')<page.indexOf('id="initialInbound"'));
 const h=stocktakeHarness();h.run(moduleSource);h.window.location.search='?location_id=42&return_scan=1&return_product=abcdef012345abcdef012345';h.run('renderSelectedLocationIdentity()');assert.equal(h.get('openCurrentMap').textContent,'返回扫码货位');h.run('openWarehouseMap()');assert.equal(h.data.redirects.at(-1),'/q/42/abcdef012345abcdef012345');
});

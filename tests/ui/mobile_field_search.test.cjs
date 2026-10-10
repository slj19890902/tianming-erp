const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {parseHTML}=require('linkedom');
const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'static/mobile_erp.html'),'utf8');
const fieldSource=fs.readFileSync(path.join(root,'static/ui/mobile-field-search.js'),'utf8');
const returnSource=fs.readFileSync(path.join(root,'static/ui/mobile-field-return.js'),'utf8');

function modules(){
  const window={};
  vm.runInNewContext(fieldSource,{window,URLSearchParams});
  vm.runInNewContext(returnSource,{window,URLSearchParams,Uint8Array});
  return window;
}
function storage(){const map=new Map();return {get length(){return map.size},key(i){return [...map.keys()][i]??null},getItem(key){return map.get(key)??null},setItem(key,value){map.set(key,value)},removeItem(key){map.delete(key)}}}

test('phone page compiles and keeps one primary input with three intents and old grouped search',()=>{
  const inline=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match=>match[1]);
  for(const script of inline)new vm.Script(script);
  assert.equal((html.match(/id="lookupInput"/g)||[]).length,1);
  for(const intent of ['product','board','location'])assert.match(html,new RegExp(`data-field-intent="${intent}"`));
  assert.match(html,/runUnifiedSearch\(1\)/);
  assert.match(html,/查看全部.*group\.label/);
  assert.match(html,/fieldOtherResults/);
  assert.match(html,/fieldNearbyPager/);
  assert.match(html,/TmDimensionStock\?\.mount/);
});

test('measure parser supports finished 3D and board 2D but rejects malformed or oversized input',()=>{
  const field=modules().TmMobileFieldSearch;
  assert.equal(JSON.stringify(field.dimensions('420×310×260')),JSON.stringify({length:'420',width:'310',height:'260'}));
  assert.equal(JSON.stringify(field.dimensions('800 x 600 mm')),JSON.stringify({length:'800',width:'600'}));
  for(const query of ['800','800×0','800×600×0','100001×600','800×600<script>'])assert.equal(field.dimensions(query),null);
});

test('location projection never treats undisclosed default as empty stock and links are local read-only',()=>{
  const field=modules().TmMobileFieldSearch;
  const rows=field.locations({items:[{location_id:42,location_name:'内部码',employee_location_name:'B7',floor_code:'1F',area_code:'M',default_binding:null}],total:1,page:1,has_more:false});
  assert.equal(rows[0].name,'B7');assert.equal(rows[0].binding,null);
  assert.match(rows[0].href,/^\/warehouse\.html\?embedded=1/);
  assert.match(rows[0].href,/readonly=1/);
  assert.equal(field.locationHref({location_id:42,floor_code:'https://evil'}),null);
  assert.throws(()=>field.locations({items:[],total:'1',page:1,has_more:false}));
});

test('field return is actor-bound and accepts only a local lookup target',()=>{
  const nav=modules().TmMobileFieldReturn,s=storage();
  const token=nav.capture(s,17,{kind:'field',query:'800×600',includeZero:false,selectedProductId:23,locationId:42,floorCode:'1F',areaCode:'M',scrollY:250,intent:'board',page:3,category:'inventory',lookupPage:2,nearPage:4},1000);
  assert.match(token,/^[a-f0-9]{32}$/);
  const saved=nav.read(s,17,token,1000);assert.equal(saved.intent,'board');assert.equal(saved.page,3);assert.equal(saved.category,'inventory');assert.equal(saved.lookupPage,2);assert.equal(saved.nearPage,4);
  assert.equal(saved.fieldTolerance,'5');assert.equal(saved.fieldMaterial,'');assert.equal(saved.fieldFlute,'');assert.equal(saved.fieldBoardState,'raw');
  assert.equal(nav.read(s,18,token,1000),null);
  const target=nav.target(new URLSearchParams(`return_source=field&return_context=${token}&return_url=https://evil.test`));
  assert.equal(target.href,`/mobile/?return_source=field&return_context=${token}#lookup`);
  assert.equal(nav.target(new URLSearchParams('return_source=field&return_context=../../evil')).href,'/mobile/?return_source=field#lookup');
  assert.equal(nav.capture(s,17,{kind:'field',query:'x',includeZero:false,selectedProductId:null,locationId:null,floorCode:'',areaCode:'',scrollY:0,intent:'invalid',page:1,category:'all',lookupPage:1},1000),null);
  const conditions={kind:'field',query:'800×600',includeZero:false,selectedProductId:23,locationId:42,floorCode:'1F',areaCode:'M',scrollY:250,intent:'board',page:3,category:'inventory',lookupPage:2,nearPage:4,fieldTolerance:'10',fieldMaterial:'K纸',fieldFlute:'AB',fieldBoardState:'printed'};
  const next=nav.capture(s,17,conditions,1000);assert.ok(next);
  const recovered=nav.read(s,17,next,1000);for(const key of ['fieldTolerance','fieldMaterial','fieldFlute','fieldBoardState'])assert.equal(recovered[key],conditions[key]);
  for(const bad of [{fieldTolerance:'999'},{fieldMaterial:'x'.repeat(61)},{fieldMaterial:'bad\nvalue'},{fieldFlute:'URL'},{fieldBoardState:'moving'}])assert.equal(nav.capture(s,17,{...conditions,...bad},1000),null);
});

test('portal storage restores field conditions and discards stale or invalid account values',()=>{
  const start=html.indexOf('      function resetFieldConditions()');
  const end=html.indexOf('      function clearMobilePortalState()',start);
  assert.ok(start>0&&end>start);
  const controls={fieldTolerance:{value:'10'},fieldMaterial:{value:'K纸'},fieldFlute:{value:'AB'},fieldBoardState:{value:'printed'},lookupDimensionMode:{value:'text'},dimensionCustomer:{value:''},dimensionAxis:{value:'any'},dimensionTolerance:{value:'5',options:[{value:'5'},{value:'0'}]},dimensionControls:{hidden:true},textOrderFilters:{hidden:false}};
  const state={lookupUserId:17,activePage:'lookup',lookupQuery:'800×600',lookupCategory:'all',fieldIntent:'board',fieldLocationPage:2,lookupPage:3,expandedOrderItems:new Set(),dimensionSettings:{version:1},scrollByPage:new Map()};
  const saved=storage(),key='portal-test';
  const context={state,byId:id=>controls[id],sessionStorage:saved,mobilePortalStorageKey:key,mobilePages:new Set(['lookup']),setFieldIntent:intent=>{state.fieldIntent=intent}};
  const methods=vm.runInNewContext(`${html.slice(start,end)}\n({persistMobilePortalState,restoreMobilePortalState})`,context);
  methods.persistMobilePortalState();
  Object.assign(controls.fieldTolerance,{value:'0'});controls.fieldMaterial.value='';controls.fieldFlute.value='';controls.fieldBoardState.value='raw';
  assert.equal(methods.restoreMobilePortalState(17),'lookup');
  assert.equal(controls.fieldTolerance.value,'10');assert.equal(controls.fieldMaterial.value,'K纸');assert.equal(controls.fieldFlute.value,'AB');assert.equal(controls.fieldBoardState.value,'printed');
  const older=JSON.parse(saved.getItem(key));delete older.field_tolerance;delete older.field_material;delete older.field_flute;delete older.field_board_state;saved.setItem(key,JSON.stringify(older));
  methods.restoreMobilePortalState(17);
  assert.equal(controls.fieldTolerance.value,'5');assert.equal(controls.fieldMaterial.value,'');assert.equal(controls.fieldFlute.value,'');assert.equal(controls.fieldBoardState.value,'raw');
  saved.setItem(key,JSON.stringify({...older,field_material:'bad\nvalue',field_board_state:'evil'}));methods.restoreMobilePortalState(17);
  assert.equal(controls.fieldMaterial.value,'');assert.equal(controls.fieldBoardState.value,'raw');
  controls.fieldMaterial.value='old actor';assert.equal(methods.restoreMobilePortalState(99),'');assert.equal(controls.fieldMaterial.value,'');
});

test('stocktake return restores new and legacy board conditions before the search',async()=>{
  const start=html.indexOf('      async function restoreFieldLaunch('),end=html.indexOf('      async function restoreWarehouseSearch(',start);
  assert.ok(start>0&&end>start);
  const nav=modules().TmMobileFieldReturn,s=storage(),base={kind:'field',query:'800×600',includeZero:false,selectedProductId:null,locationId:42,floorCode:'1F',areaCode:'M',scrollY:0,intent:'board',page:1,category:'all',lookupPage:1,nearPage:1};
  const controls={lookupInput:{value:''},fieldTolerance:{value:'5'},fieldMaterial:{value:''},fieldFlute:{value:''},fieldBoardState:{value:'raw'}};
  const state={shell:{user:{id:17}},activePage:'lookup',fieldLocationPage:1},seen=[];
  const window={TmMobileFieldReturn:nav,sessionStorage:s,requestAnimationFrame(){},scrollTo(){}};
  const context={window,state,byId:id=>controls[id],warehouseActor:()=>17,setFieldIntent:intent=>{state.fieldIntent=intent},persistMobilePortalState(){},runFieldSearch:async(...args)=>{seen.push({args,values:Object.fromEntries(['fieldTolerance','fieldMaterial','fieldFlute','fieldBoardState'].map(key=>[key,controls[key].value]))})}};
  const restore=vm.runInNewContext(`${html.slice(start,end)}\nrestoreFieldLaunch`,context);
  for(const values of [{fieldTolerance:'10',fieldMaterial:'K纸',fieldFlute:'AB',fieldBoardState:'printed'},{}]){
    const token=nav.capture(s,17,{...base,...values});await restore(new URLSearchParams({return_context:token}));
  }
  assert.equal(JSON.stringify(seen.map(row=>row.values)),JSON.stringify([{fieldTolerance:'10',fieldMaterial:'K纸',fieldFlute:'AB',fieldBoardState:'printed'},{fieldTolerance:'5',fieldMaterial:'',fieldFlute:'',fieldBoardState:'raw'}]));
  assert.equal(seen[0].args[0],1);assert.equal(controls.lookupInput.value,'800×600');
});

test('product first view shows primary code, mold short bay and real position with permission guard',async()=>{
  const source=fs.readFileSync(path.join(root,'static/ui/product-workbench.js'),'utf8');
  const {window}=parseHTML('<html><body><main id="root"></main></body></html>');
  const box={window:null,document:window.document,AbortController,URLSearchParams,setTimeout,clearTimeout,console};box.window=box;
  vm.createContext(box);vm.runInContext(source,box);
  const stock=()=>({summary:Object.fromEntries(['finished','semi_finished','processed_component'].map(key=>[key,{actual:0,available:0,reserved:0,unit:'片'}])),groups:Object.fromEntries(['finished','semi_finished','processed_component'].map(key=>[key,{positions:[]}]))});
  const detail=hidden=>({product:{id:9,product_id:9,product_code:'80012273',customer_material_code:'CUST-77',product_name:'合成盒',drawings:{status:'none',items:[]}},production:{molds:[hidden?{name:'C架模具',location_visibility:'hidden_by_permission',mold_id:7}:{name:'C架模具',location_visibility:'visible',short_label:'B7',location_label:'一楼 · 模具C架 · B7（第2层第7格）',mold_id:7,map_url:'/mobile/mold-lookup?mold_id=7&readonly=1'}],bom:[{product_id:10,product_code:'CHILD',product_name:'子件',quantity:1,molds:[{name:'子件模具',location_visibility:'not_recorded'}]}],process_steps:[]},inventory:hidden?{visibility:'hidden_by_permission',items:[]}:{...stock(),items:[{location_label:'北侧 B7',actual:3,available:3,reserved:0}]},orders:{items:[]},activity:{items:[],deliveries:[],related_products:[],page:1,has_more:false},actions:{}});
  const rootNode=window.document.getElementById('root');
  const component=box.ERPProductWorkbench.mount({container:rootNode,request:async()=>detail(false),externalSearch:true});
  await component.show(9);
  const summary=rootNode.querySelector('.pw-field-summary');
  assert.match(summary.textContent,/80012273/);assert.match(summary.textContent,/B7/);assert.match(summary.textContent,/北侧 B7/);
  assert.equal(summary.querySelector('strong').textContent,'80012273');
  assert.equal(rootNode.classList.contains('pw-external-search'),true);
  assert.equal(rootNode.querySelector('.pw-tabs [data-tab="production"]').classList.contains('active'),true);
  rootNode.querySelector('.pw-tabs [data-tab="activity"]').dispatchEvent(new window.Event('click'));
  assert.equal(rootNode.querySelector('.pw-tabs [data-tab="activity"]').classList.contains('active'),true);
  await component.show(9);
  assert.equal(rootNode.querySelector('.pw-tabs [data-tab="activity"]').classList.contains('active'),true);
  component.destroy();
  const desktop=box.ERPProductWorkbench.mount({container:rootNode,request:async()=>detail(false)});
  await desktop.show(9);
  assert.equal(rootNode.querySelector('.pw-tabs [data-tab="activity"]').classList.contains('active'),true);
  desktop.destroy();
  const restored=box.ERPProductWorkbench.mount({container:rootNode,request:async()=>detail(false),externalSearch:true,initialState:{productId:9,tab:'inventory'}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(rootNode.querySelector('.pw-tabs [data-tab="inventory"]').classList.contains('active'),true);
  restored.destroy();
  const hidden=box.ERPProductWorkbench.mount({container:rootNode,request:async()=>detail(true),externalSearch:true});
  await hidden.show(9);
  assert.match(rootNode.querySelector('.pw-field-summary').textContent,/无库存查看权限/);
  assert.match(rootNode.querySelector('.pw-field-summary').textContent,/无位置查看权限/);
  assert.doesNotMatch(rootNode.querySelector('.pw-field-summary').textContent,/未查到实存货位/);
  hidden.destroy();
  const childOnly=detail(false);childOnly.production.molds=[];childOnly.production.bom[0].molds=[{mold_id:12,name:'子件模具',short_label:'B7',location_visibility:'visible'}];
  const combo=box.ERPProductWorkbench.mount({container:rootNode,request:async()=>childOnly,externalSearch:true});
  await combo.show(9);
  assert.match(rootNode.querySelector('.pw-field-summary').textContent,/CHILD/);
  assert.match(rootNode.querySelector('.pw-field-summary').textContent,/B7/);
  assert.doesNotMatch(rootNode.querySelector('.pw-field-summary').textContent,/无关联模具/);
  combo.destroy();
});

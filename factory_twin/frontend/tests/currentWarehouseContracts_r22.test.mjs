import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
import ts from 'typescript';
import * as moldView from '../src/moldRackView.mjs';
import * as moldPrint from '../src/moldRackPrint.mjs';
import {filterShelfMolds} from '../src/shelfDisplay.mjs';

const read=p=>fs.readFileSync(new URL(p,import.meta.url),'utf8');
const html=read('../../../static/index.html');
const warehouse=read('../src/WarehouseTwinApp.tsx');
const mobile=read('../../../static/mobile_erp.html');
const stocktake=read('../../../static/mobile_stocktake.html');
const compile=s=>ts.transpileModule(s,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
function mobileFunction(name, source=mobile){
 const start=source.indexOf(`      function ${name}(`);
 assert.ok(start>=0,name);
 return source.slice(start,source.indexOf('\n      }',start)+8);
}

test('Current PDF pagination keeps thirty editable rows, stable order and the original source objects',()=>{
 const scope={};vm.runInNewContext(read('../../../static/ui/pdf-workspace.js'),scope);
 let mixin;scope.ERPPdfWorkspace.install({mixin:d=>mixin=d});
 const ctx={...mixin.methods,pdfFitCapacity:2};
 const items=Array.from({length:61},(_,i)=>({client_line_id:`line-${i+1}`,quantity:i+1,unit_price:'3.600000'}));
 const before=JSON.stringify(items),draft={items,_product_page:2};
 assert.equal(ctx.pdfPageSize(),30);
 assert.equal(ctx.pdfPageItems(draft).length,30);
 assert.equal(ctx.pdfPageItems(draft)[0],items[30]);assert.equal(ctx.pdfPageItems(draft)[29],items[59]);
 draft._product_page=3;assert.equal(ctx.pdfPageItems(draft)[0],items[60]);
 draft._product_page=99;assert.equal(ctx.pdfPageItems(draft)[0],items[60]);
 assert.equal(draft._product_page,99);assert.equal(JSON.stringify(items),before);
 assert.equal(scope.ERPPdfWorkspace.amount({quantity:5,unit_price:''}),null);
});

test('Current shelf labels keep the readable hyphen address and escape all product text without printing quantity',()=>{
 const source=read('../../../static/shelf-label.js');
 const h=v=>String(v??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
 const scope={h,lot:null,$:()=>({value:'cell'})};vm.createContext(scope);
 const part=source.slice(source.indexOf('const productAddress ='),source.indexOf('async function load(')).replace(/let busy = false;[\s\S]*?(?=function label\()/,'');
 vm.runInContext(part,scope);
 const row={title:'三楼 一号货架',position:'2层 3格',compact_title:'A1',compact_position:'2层 3格',qr_data_url:'QR'};
 const empty=scope.label(row);assert.ok(empty.includes('class="location"'));
 for(const label of ['客户：','编码：','品名：','规格：'])assert.ok(!empty.includes(label));
 const product=scope.label({...row,product:{customer:'虚构客户',code:'001-TEST',name:'纸箱<script>',specification:'400×300×200',quantity:987654}});
 for(const expected of ['A1-2层-3格','虚构客户','001-TEST','400×300×200','QR'])assert.ok(product.includes(expected),expected);
 assert.ok(!product.includes('987654'));assert.ok(!product.includes('<script>'));
 assert.ok(product.includes('纸箱&lt;script&gt;'));assert.ok(!product.includes('三楼'));
});

test('Current shared cost resource versions match across all live desktop and mobile entry points',()=>{
 const paths=['factory_twin/frontend/warehouse-twin.html','static/factory-twin-assets/warehouse-twin.html','static/factory-twin-assets/warehouse-costs.html','static/mobile_stocktake.html','static/mobile_erp.html','static/warehouse.html'];
 const refs=paths.map(path=>{
  const source=read('../../../'+path),matches=[...source.matchAll(/warehouse-costs\.js\?v=([^"'<>\s]+)/g)];
  assert.equal(matches.length,1,path);assert.ok(!source.includes('warehouse-costs.js?v=20260911-3'),path);
  return matches[0][1];
 });
 assert.equal(new Set(refs).size,1);assert.equal(refs[0],'20261006-cost-read');
 assert.ok(read('../../../static/factory-twin-assets/warehouse-costs.js').length>100);
});

test('Current receipt storage section compiles and retains role, loading, version and idempotency controls',()=>{
 for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))if(match[1].trim())new vm.Script(match[1]);
 const start=html.indexOf('<section v-if="productForm.id" :key="`storage-');assert.ok(start>0);
 const fragment=html.slice(start,html.indexOf('</section>',start)+10);
 const scope={console};vm.createContext(scope);vm.runInContext(read('../../../static/vendor/vue-3.5.40.global.prod.js'),scope);
 const errors=[];const render=scope.Vue.compile(fragment,{decodeEntities:raw=>raw,onError:e=>errors.push(e.message)});
 assert.equal(typeof render,'function');assert.deepEqual(errors,[]);
 assert.ok(fragment.includes("['admin','boss'].includes(user?.role)"));assert.ok(fragment.includes('receiptStorage.saving'));
 assert.ok(fragment.includes('receiptStorage.product_id===productForm.id'));assert.ok(fragment.includes('receiptStorage.loading'));
 assert.ok(html.includes('idempotency_key:`receipt-storage:${id}:${createIdempotencyKey()}`'));
 assert.ok(html.includes('expected_version:state.version'));
 assert.ok(html.includes('address_version:loc?.address_version||null,layout_version:loc?.layout_version||null'));
});

test('Current mobile return restores exact location, floor, area and lot, and pending saves block scan-page exit',()=>{
 const line=stocktake.split('\n').find(s=>s.includes('function warehouseMapHref('));assert.ok(line);
 const start=stocktake.indexOf('    function restoreWarehouseAreaReturn()');
 const restore=stocktake.slice(start,stocktake.indexOf('    async function init()',start));
 const listeners={},link={addEventListener:(type,fn)=>listeners[type]=fn},backButton={addEventListener:(type,fn)=>listeners['button-'+type]=fn};
 const assigned=[],state={submitting:false,selectedLocation:{id:92,floor_code:'4F',area_code:'B7'}};
 const scope={state,inbound:{busy:false,attempt:null},URLSearchParams,window:{location:{search:'?return_floor=3F&return_area=A1&location_id=91',assign:u=>assigned.push(u)}},document:{querySelector:()=>link},$:()=>backButton,openWarehouseMap:()=>assigned.push('map')};
 const pick=stocktake.split('\n').find(s=>s.includes('const pick=(row,keys,fallback=null)=>'));assert.ok(pick);
 vm.createContext(scope);vm.runInContext(pick+'\n'+line+'\n'+restore,scope);
 const target=new URL(scope.warehouseMapHref(15),'http://fixture');
 assert.equal(target.pathname,'/mobile/');assert.equal(target.hash,'#warehouse');
 assert.equal(target.searchParams.get('floor_code'),'4F');assert.equal(target.searchParams.get('area_code'),'B7');assert.equal(target.searchParams.get('location_id'),'92');assert.equal(target.searchParams.get('lot_id'),'15');
 scope.restoreWarehouseAreaReturn();assert.equal(link.textContent,'返回货架 / 区域');
 assert.equal(new URL(link.href,'http://fixture').searchParams.get('focus_only'),'1');
 scope.window.location.search='?return_scan=1&location_id=91';scope.restoreWarehouseAreaReturn();assert.equal(link.href,'/q/91');
 const event={preventDefault(){},stopImmediatePropagation(){}};
 state.submitting=true;listeners.click(event);assert.deepEqual(assigned,[]);
 state.submitting=false;scope.inbound.attempt={};listeners.click(event);assert.deepEqual(assigned,[]);
 scope.inbound.attempt=null;listeners.click(event);assert.deepEqual(assigned,['/q/91']);
});

test('Current mobile rack preview shows customer and quantity before code, with formal positions unchanged',()=>{
 const old=read('./mobile-rack-fit.test.mjs');
 const classSource=old.slice(old.indexOf('class Element {'),old.indexOf("test('real render"));
 const elements=new Map();let Element;
 const scope={};vm.runInNewContext(classSource+';this.Element=Element;',scope);Element=scope.Element;
 const byId=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 byId('warehouseMapStage').parentElement=new Element();
 const goods=[{lot_id:18,location_id:9,customer_id:1,product_id:7,customer_short_name:'虚构甲',product_code:'001-TEST',product_name:'虚构纸箱',specification:'400×300',quantity_total:8,unit:'个'}];
 const data={map_status:'ready',locations:[{location_id:9,map_rack_id:'rack-a',rack_display_name:'A1',level_no:2,slot_no:3,can_select_target:true,goods}]},before=JSON.stringify(data);
 const state={warehouseMapData:data,warehouseSelectedRack:'rack-a',warehouseMapZoom:1};
 const helpers=mobile.slice(mobile.indexOf('      function groupWarehouseRackGoods('),mobile.indexOf('      function renderWarehouseLocationGoods('));
 vm.runInNewContext(helpers+mobileFunction('renderWarehouseMap')+';renderWarehouseMap();',{state,byId,window:{innerHeight:844},document:{createElementNS:(_,tag)=>new Element(tag)},node:(tag,cls,text)=>Object.assign(new Element(tag),{className:cls,textContent:text}),compactWarehouseLocation:()=>'',showStatus(){}});
 const cell=byId('warehouseRackList').children[0].children[1].children[1].children[0];
 assert.equal(cell.style.gridColumn,'3');assert.equal(cell.children[0].textContent,'3格 · 有货 · 8个 · 虚构甲');
 assert.equal(cell.children[1].children[0].textContent,'虚构甲 · 8个');assert.equal(cell.children[1].children[1].textContent,'001-TEST');
 assert.equal(JSON.stringify(data),before);assert.equal(data.locations[0].location_id,9);
});

test('Current mold rack selection carries the exact rack or cell scope independently of catalog filtering and writes no print fact',async()=>{
 const old=read('./moldRackInteraction.test.mjs'),start=old.indexOf('function fixture('),end=old.indexOf("test('空格");
 const component=read('../src/MoldRackElevation.tsx');
 const compiled=ts.transpileModule(component,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const scope={vm,compiled,moldView,moldPrint,filterShelfMolds,URL,URLSearchParams};vm.runInNewContext(old.slice(start,end)+';this.fixture=fixture;this.button=button;',scope);
 const f=scope.fixture({response:{floor_code:'3F',rack:{rack_id:'rack-a',blocked_levels:[],cells:[{id:'c1',level:1,grid:1,alias:'A1'},{id:'c2',level:1,grid:2,alias:'A2'}]},items:[1,2,3].map(id=>({id,mold_name:`虚构模具${id}`,products:[],location_guide:{level:1,grid:id===3?2:1,cell_id:id===3?'c2':'c1'}})),total:3,truncated:false}});
 f.render().find(n=>n.type==='input'&&n.props.placeholder==='编码、图号、名称或客户').props.onChange({target:{value:'虚构模具1'}});
 f.render().find(n=>n.props['aria-label']==='打印A1模具标签').props.onClick();await new Promise(setImmediate);
 assert.equal(f.calls.filter(c=>c.body).length,0);assert.equal(f.windows.length,1);
 const first=new URL(f.windows[0].location.href,'http://fixture');assert.equal(first.pathname,'/static/mold-print-select.html');
 assert.equal(first.searchParams.get('floor_code'),'3F');assert.equal(first.searchParams.get('rack_id'),'rack-a');assert.equal(first.searchParams.get('cell_id'),'c1');
 assert.equal(first.searchParams.get('template_version'),'mold_80x40_v1');assert.equal(first.searchParams.has('mold_ids'),false);
 scope.button(f,'打印整架模具标签').props.onClick();await new Promise(setImmediate);
 assert.equal(f.calls.filter(c=>c.body).length,0);const whole=new URL(f.windows[1].location.href,'http://fixture');
 assert.equal(whole.searchParams.get('rack_id'),'rack-a');assert.equal(whole.searchParams.get('floor_code'),'3F');
 assert.equal(whole.searchParams.has('cell_id'),false);assert.equal(whole.searchParams.has('level'),false);assert.equal(whole.searchParams.has('grid'),false);
});

test('Current move highlights retain distinct source and target roles without mutating entities',()=>{
 const source=read('../src/EditorCanvas.tsx'),fn=source.slice(source.indexOf('function syncResultHighlights('),source.indexOf('function animateFocus('));
 const calls=[],entity={name:'unchanged'};
 const scope={clearHighlightGroup(){},addMoveOutline:(group,object,role)=>calls.push({object,role}),addEntityHighlight(){},addEntityWarning(){}};
 vm.createContext(scope);vm.runInContext(compile(fn),scope);
 const runtime={resultHighlight:{},entityNodes:new Map([['pallet:source',entity],['pallet:target',entity]]),requestRender(){}};
 scope.syncResultHighlights(runtime,[],[],'target',undefined,undefined,['source']);
 assert.deepEqual(calls.map(c=>c.role),['source','target']);assert.ok(calls.every(c=>c.object===entity));assert.equal(entity.name,'unchanged');
 calls.length=0;scope.syncResultHighlights(runtime,[],[],undefined);assert.equal(calls.length,0);
});

test('Current read-only order deep link focuses the exact physical lot, and stale or zero lots cannot be shown as stock',()=>{
 const old=read('./warehouseSearchRack.test.mjs');
 const namesStart=old.indexOf('  const names=',old.indexOf("test('order location"));
 assert.ok(namesStart>0);
 const body=warehouse.indexOf('    if (pendingLocationId === null) return;'),begin=warehouse.lastIndexOf('  useEffect(() => {',body),end=warehouse.indexOf('  useEffect(() => {',body);
 const state={},lot={lot_id:15,product_id:7,quantity:20,unit:'个',inventory_code:'001-TEST'},location={location_id:91,floor_code:'3F',area_code:'A1',position_status:'mapped',storage_type:'rack'};
 const names=['Selected','CameraFocusTarget','PendingLocationId','PendingLotId','RackFocusId','PendingRackSearchLocationId','TraceDeepLinkMessage','TraceFocusedLotId','LocationItemsExpanded','SearchError','FocusedSearchItem','FocusedSearchProductKey','SearchPanelOpen'];
 const scope={productionMapContext:false,pendingLocationId:91,pendingLotId:15,pendingRackSearchLocationId:null,floorCode:'3F',dashboard:{},layout:{floor_code:'3F',racks:[{id:'rack-a'}]},loading:false,visualLocations:[location],selected:{kind:'pallet',id:'erp-location-91'},selectedLocationItems:[lot],traceReadOnly:true,focusedSearchItem:null,cameraFocusSequenceRef:{current:0},useEffect:fn=>fn(),searchRackForLocation:()=>({id:'rack-a'}),rackLocationInventoryItems:r=>r.items,inventoryHasPhysicalQuantity:r=>r.quantity>0,employeeLocationName:()=> '三楼 A1 2层3格',inventoryLabelQuantity:r=>r.quantity,inventoryUnitLabel:s=>s,formatNumber:String,searchProductKey:r=>`p-${r.product_id}`,...Object.fromEntries(names.map(name=>['set'+name,v=>state[name]=v]))};
 const code=compile(warehouse.slice(begin,end));vm.runInNewContext(code,scope);
 assert.equal(state.RackFocusId,'rack-a');assert.equal(state.TraceFocusedLotId,15);assert.equal(state.FocusedSearchItem.lot_id,15);assert.equal(state.FocusedSearchItem.location_id,91);assert.equal(state.FocusedSearchProductKey,'p-7');assert.match(state.TraceDeepLinkMessage,/黄色标记/);
 for(const items of [[],[{...lot,quantity:0}]]){Object.keys(state).forEach(k=>delete state[k]);scope.selectedLocationItems=items;vm.runInNewContext(code,scope);assert.equal(state.FocusedSearchItem,undefined);assert.equal(state.TraceFocusedLotId,undefined);assert.match(state.TraceDeepLinkMessage,/已移位、清零/);}
 assert.equal(location.location_id,91);assert.equal(lot.quantity,20);
});

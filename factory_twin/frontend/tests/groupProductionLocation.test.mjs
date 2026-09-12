import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
const source=fs.readFileSync(new URL('../../../static/ui/production-workspace.js',import.meta.url),'utf8');
function fixture(text=source){
 const calls=[],parts=[],components={},frame={};let response=async()=>{throw Error('network');};
 const sandbox={Uint8Array,URLSearchParams,crypto:{getRandomValues:bytes=>webcrypto.getRandomValues(bytes)},location:{origin:'http://erp.test'},document:{querySelector:()=>null},axios:{post:async(url,data)=>{calls.push({...data});return response();}}};
 sandbox.window=sandbox;
 vm.runInNewContext(text,sandbox);sandbox.ERPProductionWorkspace.install({mixin:p=>parts.push(p),component:(n,c)=>components[n]=c});
 const ctx={...parts[0].methods,stockPrepDialog:{row:{entry_type:'kit',plan:{recipe:{parent_id:205}}},sets:5,location:4,preview:{sets:5,basis_hash:'basis',shortages:[]},loading:false},authGeneration:1,canAdmin:true,hasPermission:()=>true,productionLocations:[{id:4,layout_version:2}],$refs:{stockLocationFrame:{contentWindow:frame}},errorMessage:e=>e.message,ensureProductionLocations:async()=>true,loadStockPreparation:async()=>{},showToast:()=>{}};
 return {ctx,calls,components,frame,setResponse:r=>response=r};
}
test('HTTP without randomUUID submits, reports errors and reuses key after uncertain request',async()=>{
 const f=fixture();await f.ctx.saveStockDialog('plan');await f.ctx.saveStockDialog('plan');assert.equal(f.calls.length,2);assert.equal(f.calls[0].operation_key,f.calls[1].operation_key);assert.equal(f.ctx.stockPrepDialog.error,'network');assert.equal(f.ctx.stockPrepDialog.loading,false);
 f.ctx.stockPrepDialog.location=5;await f.ctx.saveStockDialog('plan');assert.notEqual(f.calls[1].operation_key,f.calls[2].operation_key);
 f.setResponse(async()=>({data:{ok:true}}));await f.ctx.saveStockDialog('plan');assert.equal(f.ctx.stockPrepDialog,null);
});
test('concurrent submit and invalid preview cannot send duplicate or invalid actions',async()=>{
 const f=fixture();let release;f.setResponse(()=>new Promise(r=>release=r));const pending=f.ctx.saveStockDialog('plan');await f.ctx.saveStockDialog('plan');assert.equal(f.calls.length,1);release({});await pending;
 const g=fixture();g.ctx.stockPrepDialog.preview.shortages=[{}];await g.ctx.saveStockDialog('plan');assert.equal(g.calls.length,0);assert.match(g.ctx.stockPrepDialog.error,/补齐/);
});
test('map accepts only current frame, origin, token, draft and currently valid location',async()=>{
 const f=fixture(),d=f.ctx.stockPrepDialog;await f.ctx.openStockLocationMap();const picker=f.ctx.stockLocationMap;assert.match(picker.url,/floor=3F/);
 const event={origin:'http://erp.test',source:f.frame,data:{type:'erp-production-location',token:picker.token,location_id:4}};
 await f.ctx.acceptStockLocation({...event,origin:'http://evil.test'});assert.equal(f.ctx.stockLocationMap,picker);
 await f.ctx.acceptStockLocation({...event,source:{}});assert.equal(f.ctx.stockLocationMap,picker);
 await f.ctx.acceptStockLocation({...event,data:{...event.data,token:'wrong'}});assert.equal(f.ctx.stockLocationMap,picker);
 await f.ctx.acceptStockLocation(event);assert.equal(f.ctx.stockLocationMap,null);assert.equal(d.location,4);assert.equal(d.sets,5);assert.equal(f.calls.length,0);
 await f.ctx.openStockLocationMap();const stale=f.ctx.stockLocationMap;f.ctx.authGeneration++;await f.ctx.acceptStockLocation({...event,data:{...event.data,token:stale.token}});assert.equal(f.ctx.stockLocationMap,stale);
});
test('picker groups display names instead of EDIT/L codes, distinguishes identities and exposes exact rack levels',()=>{
 const {components}=fixture(),c=components['location-picker'];const ctx={...c.data(),...c.methods,locations:[{id:1,warehouse_floor:3,area_id:9,area_code:'EDIT-050',area_master_name:'南F货1',storage_type:'rack',map_rack_id:'r1',rack_code:'A',rack_display_name:'L003',level_no:2,slot_no:3,location_name:'南F1货架A2层3格'},{id:2,warehouse_floor:3,area_id:10,area_code:'L-B1',area_master_name:'北H货1',storage_type:'ground',location_name:'北H货1 1位'}]};for(const [k,get] of Object.entries(c.computed))Object.defineProperty(ctx,k,{get:()=>get.call(ctx)});
 assert.equal(ctx.floor,'3');assert.deepEqual(Array.from(ctx.majors),['F','H']);ctx.modelValue=1;ctx.sync();assert.equal(ctx.major,'F');assert.equal(ctx.area,'9');assert.equal(ctx.rack,'r1');assert.equal(ctx.level,'2');assert.deepEqual(Array.from(ctx.places,l=>l.id),[1]);assert.equal(ctx.rackName(ctx.locations[0]),'货架 A');
});

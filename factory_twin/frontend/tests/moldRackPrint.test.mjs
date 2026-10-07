import assert from 'node:assert/strict';
import test from 'node:test';
import {createMoldRackPrinter, moldRackPrintSelection} from '../src/moldRackPrint.mjs';

const scope = {floorCode:'1F',rackId:'rack-a'};
const mold = (id, cell='c1', extra={}) => ({id, mold_name:String(id),location_guide:{cell_id:cell,level:1,grid:cell==='c1'?1:2},...extra});
const response = items => ({floor_code:'1F',rack:{rack_id:'rack-a',cells:[{id:'c1'},{id:'c2'}]},items,total:items.length,truncated:false});
function fixture(options={}) {
  const calls=[],windows=[],states=[];let blocked=false, failure=null, confirm=true, closeOnPost=false;
  const printer=createMoldRackPrinter({
    request:async(path,body)=>{calls.push({path,body});if(body&&failure){const error=failure;failure=null;throw error;}if(body&&closeOnPost)windows.at(-1).closed=true;return body?{print_job_id:123}:response([mold(1),mold(2,'c2',options.printed?{label_print_status:{printed:true}}:{})]);},
    openWindow:url=>{if(blocked)return null;const popup={location:{href:url},closed:false,close(){this.closed=true}};windows.push(popup);return popup;},
    confirmReprint:()=>confirm,changed:state=>states.push(state),makeKey:()=> 'unique-print-key',
  });
  return{printer,calls,windows,states,block(){blocked=true},fail(error){failure=error},cancel(){confirm=false},closeDuringPost(){closeOnPost=true}};
}
test('whole rack and stable cell selection deduplicate physical IDs, omit inactive/archived, and sort',()=>{
  const rows=[mold(2,'c2'),mold(1),mold(1),mold(3,'c1',{is_active:false}),mold(4,'c1',{archive_status:'archived'})];
  assert.deepEqual(moldRackPrintSelection(response(rows),scope).map(m=>m.id),[1,2]);
  assert.deepEqual(moldRackPrintSelection(response(rows),{...scope,cellId:'c2'}).map(m=>m.id),[2]);
  assert.deepEqual(moldRackPrintSelection(response(rows),{...scope,level:1,grid:2}).map(m=>m.id),[2]);
});
test('incomplete, wrong rack, removed cell, empty and oversized ranges fail visibly',()=>{
  for(const result of [{...response([mold(1)]),truncated:true},{...response([mold(1)]),total:2},{...response([mold(1)]),floor_code:'3F'}])assert.throws(()=>moldRackPrintSelection(result,scope));
  assert.throws(()=>moldRackPrintSelection(response([mold(1)]),{...scope,cellId:'deleted'}));
  assert.throws(()=>moldRackPrintSelection(response([]),scope),/没有可打印/);
  assert.throws(()=>moldRackPrintSelection(response(Array.from({length:101},(_,i)=>mold(i+1))),scope),/单次最多100/);
});
test('fresh unfiltered rack read creates frozen job and opens the existing mobile-QR label page',async()=>{
  const f=fixture();await f.printer.run({...scope,cellId:'c2'},'mold_40x30_v1');
  assert.equal(f.calls[0].path,'/api/warehouse/molds/by-map-rack?floor_code=1F&rack_id=rack-a');
  assert.deepEqual(f.calls[1].body,{mold_ids:[2],source:'batch',template_version:'mold_40x30_v1',idempotency_key:'unique-print-key'});
  assert.equal(f.windows[0].location.href,'/mold-label.html?mold_ids=2&template_version=mold_40x30_v1&print_job_id=123');
  assert.equal(f.states.at(-1).pending,false);
});
test('popup block and cancelled reprint cannot register',async()=>{
  const f=fixture();f.block();await f.printer.run(scope);assert.equal(f.calls.length,0);
  const g=fixture({printed:true});g.cancel();await g.printer.run(scope);assert.equal(g.calls.length,1);assert.equal(g.windows[0].closed,true);
});
test('uncertain response retries the exact frozen payload/key and blocks different scopes',async()=>{
  const f=fixture();f.fail(new Error('network'));await f.printer.run(scope);
  assert.equal(f.states.at(-1).pending,true);
  await f.printer.run({...scope,cellId:'c1'});assert.equal(f.calls.length,2);
  await f.printer.retry();assert.equal(f.calls.length,3);
  assert.deepEqual(f.calls[1],f.calls[2]);assert.equal(f.states.at(-1).pending,false);
});
test('closed popup after committed job reopens original job without registration',async()=>{
  const f=fixture();f.closeDuringPost();await f.printer.run(scope);
  assert.equal(f.states.at(-1).pending,true);await f.printer.retry();
  assert.equal(f.calls.length,2);assert.match(f.windows[1].location.href,/print_job_id=123/);assert.equal(f.states.at(-1).pending,false);
});
test('double click is serialized and permission denial clears retry state',async()=>{
  const f=fixture();await Promise.all([f.printer.run(scope),f.printer.run(scope)]);assert.equal(f.calls.length,2);assert.equal(f.windows.length,1);
  const g=fixture();g.fail(Object.assign(new Error('权限不足'),{status:403}));await g.printer.run(scope);
  assert.equal(g.states.at(-1).pending,false);assert.ok(g.states.some(s=>s.message==='权限不足'));assert.equal(g.windows[0].closed,true);
});

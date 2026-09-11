import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_initial_ui_keeps_retry_payload_and_rejects_stale_search():
    source = (ROOT / "static/mobile_initial_stocktake_runtime.js").read_text(encoding="utf-8")
    harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, {replaceChildren(){},value:'', checked:false, disabled:false, classList:{add(){},remove(){},toggle(){}}}); return nodes.get(id); };
const state = {user:{id:1,role:'admin'}, selectedLocation:{id:2,layout_version:1}, lots:[], locked:false};
let mode='race', calls=[], deferred=[];
const context = {console, URLSearchParams, Intl, Date, JSON, Number, $, state, h: x => String(x),
  document: {querySelectorAll(){return []}},
  window: {location: {search: ''}},
  pick:(row,keys)=>keys.map(k=>row?.[k]).find(x=>x!==undefined),
  idempotencyKey:()=> 'stable-key', updateSubmitState(){}, showMessage(){},
  sessionStorage:{getItem(){return null},setItem(){},removeItem(){}},
  openLocation:async()=>{},
  api:async (url,options)=>{
    if(mode==='race') return await new Promise(resolve=>deferred.push(resolve));
    calls.push(JSON.parse(options.body));
    if(calls.length===1) throw new Error('lost response');
    return {idempotent_replay:true};
  }
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
(async()=>{
  $('inboundProduct').value='11';
  const first=vm.runInContext('refreshInboundContext()',context);
  $('inboundProduct').value='12';
  const second=vm.runInContext('refreshInboundContext()',context);
  deferred[1]({can_add:true,snapshot:'new',existing_quantity:0,existing_location_count:0});
  await second;
  deferred[0]({can_add:true,snapshot:'old',existing_quantity:100,existing_location_count:1});
  await first;
  assert.equal(vm.runInContext('inbound.context.snapshot',context),'new');
  mode='save';
  $('inboundCustomer').value='4'; $('inboundQuantity').value='13'; $('inboundDate').value='2026-09-08';
  await vm.runInContext('saveInitialInbound()',context);
  assert.equal($('inboundFields').disabled,true);
  assert.equal($('backToLocations').disabled,true);
  $('inboundQuantity').value='999'; // Even a scripted edit must not replace an uncertain attempt.
  await vm.runInContext('saveInitialInbound()',context);
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(calls[1].items[0].quantity,13);
  assert.equal(vm.runInContext('inbound.attempt',context),null);
  calls=[];
  state.selectedLocation.address_version=2;state.selectedLocation.published_map_revision='map-1';
  vm.runInContext("inbound.stockLot={lot_id:20,lot_version:3,quantity_movable:10,registered_location:{location_id:9,is_pending_relocation:true}}",context);
  $('inboundQuantity').value='6';
  await vm.runInContext('saveInitialInbound()',context);
  assert.equal(vm.runInContext('inbound.attempt.move_path',context),'/api/warehouse/twin-operations/pending-lots/20/place');
  $('inboundQuantity').value='10';
  await vm.runInContext('saveInitialInbound()',context);
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(calls[1].quantity,6);
  assert.equal(calls[1].expected_version,3);
  assert.equal(calls[1].expected_map_revision,'map-1');

})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node", "-e", "const SOURCE=" + json.dumps(source) + ";\n" + harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

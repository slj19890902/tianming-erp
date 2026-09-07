from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "factory_twin" / "frontend"


def _run(tmp_path, scenario):
    script = tmp_path / "match-action.mjs"
    script.write_text(r"""
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(PACKAGE);
const ts=require('typescript');
const app=readFileSync(SOURCE,'utf8');
const start=app.indexOf('  const clearRejectedStocktakeMatch = () => {');
const end=app.indexOf('  const openPendingLotRelocation = () => {',start);
assert.ok(start>0 && end>start);
const methods=ts.transpileModule(app.slice(start,end),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
function setup(){
  const posts=[];
  const env={
    canStocktake:true,authActorId:41,actorId:41,stocktakeMatch:null,stocktakeMatchBusy:false,
    stocktakeMatchRef:{current:{request:null,busy:false}},reads:0,messages:[],
    selectedStocktakeItem:{lot_id:7,lot_number:'S07-MOCK',product_name:'虚构货物',inventory_type:'finished',
      unit:'boxes',version:3,status:'active',available_quantity:8,reserved_quantity:2,damaged_quantity:1},
    selectedLocation:{location_id:12,location_name:'虚构 E1-2',map_position:{version:4},address_version:2,published_map_revision:'map-1'},
    selectedLocationStocktakeBlockReason:null,
    setStocktakeMatch(value){env.stocktakeMatch=value;},setStocktakeMatchBusy(value){env.stocktakeMatchBusy=value;},
    setWarehouseOperationMessage(value){env.messages.push(value);},formatNumber:value=>String(value),inventoryUnitLabel:unit=>unit,
    employeeLocationName:item=>item.location_name,operationKey:()=> 'match-key-1',
    window:{confirm(){throw new Error('unexpected second confirmation');}},
    requestJson:async()=>({user:{id:env.actorId},permissions:['warehouse.stocktake.submit']}),
    mutateJson:async(url,method,payload)=>{posts.push({url,method,payload});return {items:[]};},
    refreshDashboard:async()=>{env.reads++;},
  };
  return {env,posts,...new Function('environment','with(environment){'+methods+';return {confirmStocktakeMatch,clearRejectedStocktakeMatch};}')(env)};
}
SCENARIO
process.stdout.write('ok');
""".replace("PACKAGE", json.dumps(str(FRONTEND / "package.json")))
        .replace("SOURCE", json.dumps(str(FRONTEND / "src" / "WarehouseTwinApp.tsx")))
        .replace("SCENARIO", scenario), encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(script)], capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_match_one_click_freezes_selected_quantity_without_another_confirmation(tmp_path):
    _run(tmp_path, """
const c=setup();assert.equal(c.posts.length,0);
await c.confirmStocktakeMatch();
assert.equal(c.posts.length,1);assert.equal(c.env.stocktakeMatch.quantity,11);
assert.equal(c.posts[0].payload.expected_available,8);assert.equal(c.posts[0].payload.expected_reserved,2);
assert.equal(c.posts[0].payload.expected_damaged,1);assert.equal(c.posts[0].payload.expected_address_version,2);
assert.ok(Object.isFrozen(c.posts[0].payload));assert.equal(c.env.stocktakeMatch.complete,true);
assert.match(c.env.messages[0],/S07-MOCK.*核对相符.*11/);
await c.confirmStocktakeMatch();assert.equal(c.posts.length,1);
""")


def test_match_busy_and_unknown_results_keep_the_same_request(tmp_path):
    _run(tmp_path, """
for(const status of [undefined,408,425,429]){
  const c=setup();let release;
  c.env.mutateJson=(url,method,payload)=>{c.posts.push({url,method,payload});return new Promise((resolve,reject)=>{release=reject;});};
  const first=c.confirmStocktakeMatch();await Promise.resolve();await Promise.resolve();
  await c.confirmStocktakeMatch();assert.equal(c.posts.length,1);
  release(Object.assign(new Error('unknown'),{status}));await first;
  const frozen=c.env.stocktakeMatch.payload;assert.equal(c.env.stocktakeMatch.rejected,false);
  c.clearRejectedStocktakeMatch();assert.equal(c.env.stocktakeMatch.payload,frozen);
  c.env.selectedStocktakeItem={...c.env.selectedStocktakeItem,lot_id:99,version:8};
  c.env.mutateJson=async(url,method,payload)=>{c.posts.push({url,method,payload});return {items:[]};};
  await c.confirmStocktakeMatch();assert.equal(c.posts[1].payload,frozen);
  assert.equal(c.posts[1].url,'/api/warehouse/twin-operations/lots/7/stocktake-match');
  assert.equal(c.env.stocktakeMatch.complete,true);
}
""")


def test_match_written_refresh_failure_never_reposts_and_unknown_then_409_stays_frozen(tmp_path):
    _run(tmp_path, """
const c=setup();c.env.refreshDashboard=async()=>{c.env.reads++;throw new Error('read failed');};
await c.confirmStocktakeMatch();assert.equal(c.env.stocktakeMatch.written,true);
assert.equal(c.env.stocktakeMatch.complete,false);assert.match(c.env.stocktakeMatch.error,/相符记录已保存/);
c.env.refreshDashboard=async()=>{c.env.reads++;};await c.confirmStocktakeMatch();
assert.equal(c.posts.length,1);assert.equal(c.env.stocktakeMatch.complete,true);
const d=setup();d.env.mutateJson=async()=>{throw new Error('unknown');};await d.confirmStocktakeMatch();
const payload=d.env.stocktakeMatch.payload;
d.env.mutateJson=async()=>{throw Object.assign(new Error('conflict'),{status:409});};await d.confirmStocktakeMatch();
assert.equal(d.env.stocktakeMatch.rejected,false);d.clearRejectedStocktakeMatch();assert.equal(d.env.stocktakeMatch.payload,payload);
""")


def test_match_actor_change_blocks_old_retry_and_success_callback_refresh(tmp_path):
    _run(tmp_path, """
const c=setup();c.env.actorId=42;await c.confirmStocktakeMatch();assert.equal(c.posts.length,0);
c.env.actorId=41;let release;
c.env.mutateJson=(url,method,payload)=>{c.posts.push({url,method,payload});return new Promise(resolve=>{release=resolve;});};
const old=c.confirmStocktakeMatch();await Promise.resolve();await Promise.resolve();c.env.actorId=42;
release({items:[]});await old;assert.equal(c.env.reads,0);assert.equal(c.env.stocktakeMatchBusy,false);
await c.confirmStocktakeMatch();assert.equal(c.posts.length,1);assert.equal(c.env.stocktakeMatch.written,true);
""")

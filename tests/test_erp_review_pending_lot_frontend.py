from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "factory_twin" / "frontend"


def _run(tmp_path, scenario):
    node = shutil.which("node")
    assert node, "Node.js is required for the map action behavior test"
    script = tmp_path / "pending-lot-action.mjs"
    script.write_text(r"""
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require = createRequire(FRONTEND_PACKAGE);
const ts = require('typescript');
const app = readFileSync(FRONTEND_SOURCE,'utf8');
const start = app.indexOf('  const openPendingLotRelocation = () => {');
const end = app.indexOf('  const placePendingInventory = async () => {',start);
assert.ok(start>0 && end>start);
const methods = ts.transpileModule(app.slice(start,end), {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
function setup() {
  const posts=[];
  const env={
    canEditLocations:true, authActorId:41, actorId:41,
    selectedStocktakeItem:{lot_id:7,lot_number:'MOCK-LOT-7',product_name:'虚构货物',customer_name:'虚构客户',
      inventory_type:'finished',unit:'boxes',status:'active',version:3,available_quantity:8,reserved_quantity:2,damaged_quantity:1},
    selectedLocation:{location_id:12,location_name:'虚构 E1-11',map_position:{version:4}},
    selectedLocationStocktakeBlockReason:null,
    selectedLocationPallets:[{pallet_id:9,version:5,items:[{lot_id:7},{lot_id:8}]}],
    pendingLotRef:{current:{draft:null,busy:false}},pendingLotDraft:null,pendingLotBusy:false,reads:0,messages:[],
    setPendingLotDraft(value){env.pendingLotDraft=value;}, setPendingLotBusy(value){env.pendingLotBusy=value;},
    setStocktakeLotId(value){env.selectedLotId=value;},setStocktakeDecreaseQuantity(value){env.decrease=value;},
    setWarehouseOperationMessage(value){env.messages.push(value);},
    employeeCustomerName:item=>item.customer_name,employeeLocationName:item=>item.location_name,
    inventoryUnitLabel:unit=>unit,formatNumber:value=>String(value),operationKey:()=> 'pending-key-1',
    requestJson:async()=>({user:{id:env.actorId,role:'admin'}}),
    mutateJson:async(url,method,payload)=>{posts.push({url,method,payload});return {lot_id:7};},
    refreshDashboard:async()=>{env.reads++;},
  };
  const actions=new Function('environment','with(environment){'+methods+';return {openPendingLotRelocation,cancelPendingLotRelocation,confirmPendingLotRelocation};}')(env);
  return {env,posts,...actions};
}
SCENARIO
process.stdout.write('ok');
""".replace("FRONTEND_PACKAGE", json.dumps(str(FRONTEND / "package.json")))
        .replace("FRONTEND_SOURCE", json.dumps(str(FRONTEND / "src" / "WarehouseTwinApp.tsx")))
        .replace("SCENARIO", scenario), encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_pending_lot_cancel_is_zero_post_and_snapshot_is_the_selected_batch(tmp_path):
    _run(tmp_path, """
const c=setup();c.openPendingLotRelocation();
assert.equal(c.env.pendingLotDraft.quantity,11);
assert.equal(c.env.pendingLotDraft.lotId,7);
assert.equal(c.env.pendingLotDraft.payload.expected_pallet_id,9);
assert.equal(c.env.pendingLotDraft.payload.expected_pallet_version,5);
assert.ok(Object.isFrozen(c.env.pendingLotDraft.payload));
assert.equal(c.posts.length,0);
c.cancelPendingLotRelocation();
assert.equal(c.env.pendingLotDraft,null);assert.equal(c.posts.length,0);
""")


def test_pending_lot_busy_and_unknown_retry_keep_one_immutable_request(tmp_path):
    _run(tmp_path, """
const c=setup();c.openPendingLotRelocation();
let release;
c.env.mutateJson=(url,method,payload)=>{c.posts.push({url,method,payload});return new Promise((resolve,reject)=>{release=reject;});};
const first=c.confirmPendingLotRelocation();await Promise.resolve();await Promise.resolve();
await c.confirmPendingLotRelocation();c.cancelPendingLotRelocation();
assert.equal(c.posts.length,1);assert.equal(c.env.pendingLotBusy,true);
release(new Error('lost response'));await first;
const frozen=c.env.pendingLotDraft.payload;
c.env.selectedLocation={location_id:99,location_name:'另一位置',map_position:{version:8}};
c.env.selectedStocktakeItem={...c.env.selectedStocktakeItem,lot_id:100};
c.openPendingLotRelocation();c.cancelPendingLotRelocation();
assert.equal(c.env.pendingLotDraft.payload,frozen);assert.equal(c.env.pendingLotDraft.lotId,7);
c.env.mutateJson=async(url,method,payload)=>{c.posts.push({url,method,payload});return {lot_id:7,replayed:true};};
await c.confirmPendingLotRelocation();
assert.equal(c.posts.length,2);assert.equal(c.posts[1].payload,c.posts[0].payload);
assert.equal(c.posts[1].url,'/api/warehouse/twin-operations/lots/7/pending-relocation');
assert.equal(c.env.pendingLotDraft.complete,true);
""")


def test_pending_lot_written_refresh_failure_never_posts_again(tmp_path):
    _run(tmp_path, """
const c=setup();c.openPendingLotRelocation();
c.env.refreshDashboard=async()=>{c.env.reads++;throw new Error('read unavailable');};
await c.confirmPendingLotRelocation();
assert.equal(c.posts.length,1);assert.equal(c.env.pendingLotDraft.written,true);
assert.equal(c.env.pendingLotDraft.complete,false);assert.equal(c.env.messages.length,0);
assert.match(c.env.pendingLotDraft.error,/只需刷新/);
c.env.refreshDashboard=async()=>{c.env.reads++;};
await c.confirmPendingLotRelocation();
assert.equal(c.posts.length,1);assert.equal(c.env.reads,2);assert.equal(c.env.pendingLotDraft.complete,true);
c.cancelPendingLotRelocation();assert.equal(c.env.pendingLotDraft,null);
""")


def test_pending_lot_actor_change_blocks_retry_and_old_callback_refresh(tmp_path):
    _run(tmp_path, """
const c=setup();c.openPendingLotRelocation();c.env.actorId=42;
await c.confirmPendingLotRelocation();assert.equal(c.posts.length,0);assert.match(c.env.pendingLotDraft.error,/身份已变化/);
c.env.actorId=41;
let release;
c.env.mutateJson=(url,method,payload)=>{c.posts.push({url,method,payload});return new Promise(resolve=>{release=resolve;});};
const pending=c.confirmPendingLotRelocation();await Promise.resolve();await Promise.resolve();
c.env.actorId=42;release({lot_id:7});await pending;
assert.equal(c.env.reads,0);assert.equal(c.env.pendingLotBusy,false);assert.equal(c.env.pendingLotDraft.written,true);
await c.confirmPendingLotRelocation();assert.equal(c.posts.length,1);assert.equal(c.env.reads,0);
""")


def test_pending_lot_definitive_rejection_allows_cancel_without_writes(tmp_path):
    _run(tmp_path, """
const c=setup();c.openPendingLotRelocation();
c.env.mutateJson=async()=>{throw Object.assign(new Error('stale lot'),{status:409});};
await c.confirmPendingLotRelocation();assert.equal(c.env.pendingLotDraft.rejected,true);
assert.equal(c.env.reads,0);c.cancelPendingLotRelocation();assert.equal(c.env.pendingLotDraft,null);
""")


@pytest.mark.parametrize("status", [408, 425, 429])
def test_pending_lot_uncertain_http_status_keeps_payload_for_retry(tmp_path, status):
    _run(tmp_path, """
const c=setup();c.openPendingLotRelocation();
const frozen=c.env.pendingLotDraft.payload;
c.env.mutateJson=async(url,method,payload)=>{
  c.posts.push({url,method,payload});throw Object.assign(new Error('result pending'),{status:STATUS});
};
await c.confirmPendingLotRelocation();
assert.equal(c.env.pendingLotDraft.rejected,false);assert.equal(c.env.pendingLotDraft.written,false);
assert.match(c.env.pendingLotDraft.error,/结果尚未确认/);
c.cancelPendingLotRelocation();assert.equal(c.env.pendingLotDraft.payload,frozen);
c.env.mutateJson=async(url,method,payload)=>{c.posts.push({url,method,payload});return {lot_id:7,replayed:true};};
await c.confirmPendingLotRelocation();
assert.equal(c.posts.length,2);assert.equal(c.posts[1].payload,frozen);
assert.equal(c.posts[1].url,c.posts[0].url);assert.equal(c.env.pendingLotDraft.complete,true);
""".replace("STATUS", str(status)))

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _run(tmp_path: Path, scenario: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend method tests")
    start = INDEX.index("async supplementProductionCompletion(row)")
    end = INDEX.index("openProductionInventory(row)", start)
    methods = INDEX[start:end]
    close_start = INDEX.index('if (this.modal?.type === "productionSupplement") {', INDEX.index("closeModal() {"))
    close_end = INDEX.index("const invoiceSellerReturnCustomerId", close_start)
    close_guard = INDEX[close_start:close_end]
    watch_start = INDEX.index("authGeneration: {", INDEX.index("watch: {"))
    watch_end = INDEX.index("warehouseFrameUrl()", watch_start)
    auth_watcher = INDEX[watch_start:watch_end]
    harness = """
import assert from 'node:assert/strict';
const source = METHODS;
const closeGuard = CLOSE_GUARD;
const authWatcher = new Function('return {'+AUTH_WATCHER+'}')().authGeneration;
const row = {
  id:7, task_id:19, task_version:4, can_supplement:true,
  available_material_input_quantity:10, output_factor:2, pieces_per_box:1,
  product_code:'MOCK-1', product_name:'虚构生产补录',
};
const location = {id:8,is_empty:true,layout_version:6,pallet_id:12,pallet_code:'PL-MOCK',employee_location_name:'虚构成品位置'};
function setup() {
  const posts = [];
  let keyCount = 0;
  const axios = {post:async(url,payload)=>{posts.push({url,payload}); return {data:{}};}};
  const methods = new Function('axios','createIdempotencyKey','return {'+source+'}')(axios,()=> 'key-'+(++keyCount));
  const vm = {
    ...methods, canAdmin:true, user:{id:41}, authGeneration:2, productionBusy:false, productionSupplementForm:null,
    modal:null, productionLocationsLoading:false, productionLocationsError:'',
    locations:[{...location}], notices:[], reads:0,
    productionTheoreticalOutput(item){return Math.floor(Number(item.actual_input_quantity)*item.output_factor/item.pieces_per_box);},
    productionLocationsForRow(){return this.locations.filter(item=>item.is_empty);},
    ensureProductionLocations:async()=>true,
    inventoryLocation(item){return item.employee_location_name;},
    errorMessage(error){return error.message;},
    showToast(message){this.notices.push(message);},
    async loadProduction(){this.reads++;return true;},
    async loadDeliveries(){this.reads++;return true;},
    async loadKpi(){this.reads++;return true;},
    closeModal:new Function(closeGuard+'this.modal=null;'),
    expireSession(actorId){this.authGeneration++;authWatcher.handler.call(this,this.authGeneration);this.user={id:actorId};this.modal=null;},
  };
  return {vm,axios,posts};
}
async function ready() {
  const context=setup();
  await context.vm.supplementProductionCompletion(row);
  context.vm.productionSupplementForm.location_id=8;
  context.vm.productionSupplementForm.output=17;
  return context;
}
SCENARIO
process.stdout.write('ok');
""".replace("METHODS", json.dumps(methods, ensure_ascii=False)).replace(
        "CLOSE_GUARD", json.dumps(close_guard, ensure_ascii=False)
    ).replace("SCENARIO", scenario)
    harness = harness.replace("AUTH_WATCHER", json.dumps(auth_watcher, ensure_ascii=False))
    script = tmp_path / "production-supplement.mjs"
    script.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, encoding="utf-8",
        timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout == "ok"


@pytest.mark.parametrize("scenario", [
    """
const {vm,posts}=await ready();
assert.equal(vm.modal.type,'productionSupplement');
assert.equal(posts.length,0);
assert.deepEqual(vm.productionSupplementSummary(),{availableInput:10,theoretical:20,defective:3,destination:'虚构成品位置'});
vm.closeModal();
assert.equal(vm.modal,null);
assert.equal(vm.productionSupplementForm,null);
assert.equal(posts.length,0);
""",
    """
const {vm,posts}=await ready();
await vm.submitProductionSupplement();
assert.equal(posts.length,1);
assert.equal(posts[0].url,'/api/production/completion-batches');
assert.deepEqual(posts[0].payload.items[0],{
 task_id:19,expected_version:4,disposition:'stock',completion_type:'supplemental',
 material_input_quantity:10,actual_output_quantity:17,defective_quantity:3,
 location_id:8,expected_layout_version:6,pallet_id:12,pallet_code:'PL-MOCK',remarks:null,
});
assert.equal(vm.productionSupplementForm.submitted,true);
assert.equal(vm.productionSupplementForm.refreshed,true);
assert.equal(vm.productionBusy,false);
vm.closeModal();
await vm.supplementProductionCompletion(row);
await vm.submitProductionSupplement();
assert.equal(posts.length,1);
""",
    """
for (const [input,output,locationId] of [[0,1,8],[11,1,8],[1.5,1,8],[10,0,8],[10,21,8],[10,1.5,8],[10,17,999]]) {
 const {vm,posts}=await ready();
 Object.assign(vm.productionSupplementForm,{input,output,location_id:locationId});
 await vm.submitProductionSupplement();
 assert.equal(posts.length,0);
 assert.ok(vm.productionSupplementForm.error);
 assert.equal(vm.productionSupplementForm.payload,null);
}
const {vm,posts}=await ready(); vm.locations[0].is_empty=false;
await vm.submitProductionSupplement(); assert.equal(posts.length,0);
""",
    """
const {vm,posts}=await ready(); vm.canAdmin=false;
await vm.submitProductionSupplement(); assert.equal(posts.length,0);
vm.productionSupplementForm=null; await vm.supplementProductionCompletion(row);
assert.equal(vm.productionSupplementForm,null);
vm.canAdmin=true; await vm.supplementProductionCompletion({...row,can_supplement:false});
assert.equal(vm.productionSupplementForm,null);
""",
    """
const {vm,axios,posts}=await ready();
let finish;
axios.post=(url,payload)=>{posts.push({url,payload});return new Promise(resolve=>{finish=resolve;});};
const first=vm.submitProductionSupplement();
await vm.submitProductionSupplement();
vm.closeModal();
assert.equal(posts.length,1);
assert.equal(vm.modal.type,'productionSupplement');
assert.equal(vm.productionBusy,true);
finish({data:{}}); await first;
assert.equal(vm.productionBusy,false);
""",
    """
for (const status of [0,500,408]) {
 const {vm,axios,posts}=await ready();
 axios.post=async(url,payload)=>{posts.push({url,payload}); if(posts.length===1) throw {message:'连接中断',response:status?{status}:undefined};return {data:{}};};
 await vm.submitProductionSupplement();
 const payload=vm.productionSupplementForm.payload;
 assert.ok(Object.isFrozen(payload) && Object.isFrozen(payload.items) && Object.isFrozen(payload.items[0]));
 assert.equal(vm.productionSupplementCloseBlocked(),true);
 vm.closeModal(); assert.equal(vm.modal.type,'productionSupplement');
 Object.assign(vm.productionSupplementForm,{input:1,output:1,location_id:999});
 vm.locations=[];
 await vm.supplementProductionCompletion({...row,task_id:99});
 assert.equal(vm.productionSupplementForm.row.task_id,19);
 await vm.submitProductionSupplement();
 assert.equal(posts.length,2);
 assert.strictEqual(posts[1].payload,payload);
 assert.equal(posts[1].payload.items[0].actual_output_quantity,17);
 assert.equal(vm.productionSupplementForm.submitted,true);
}
""",
    """
const {vm,posts}=await ready();
vm.loadProduction=async()=>{throw new Error('读取失败');};
await vm.submitProductionSupplement();
assert.equal(posts.length,1);
assert.equal(vm.productionSupplementForm.submitted,true);
assert.equal(vm.productionSupplementForm.refreshed,false);
assert.match(vm.productionSupplementForm.error,/补录已成功/);
vm.loadProduction=async()=>true;
await vm.submitProductionSupplement();
assert.equal(posts.length,1);
assert.equal(vm.productionSupplementForm.refreshed,true);
""",
    """
const {vm,axios,posts}=await ready();
axios.post=async(url,payload)=>{posts.push({url,payload});throw {message:'数量已变化',response:{status:409}};};
await vm.submitProductionSupplement();
assert.equal(vm.productionSupplementForm.payload,null);
assert.equal(vm.productionSupplementCloseBlocked(),false);
assert.match(vm.productionSupplementForm.error,/补录未成功/);
vm.closeModal(); assert.equal(vm.productionSupplementForm,null);
""",
    """
const {vm,axios,posts}=await ready();
axios.post=async(url,payload)=>{posts.push({url,payload});throw new Error('连接中断');};
await vm.submitProductionSupplement();
const oldForm=vm.productionSupplementForm;
const oldPayload=oldForm.payload;
vm.user={id:42}; vm.authGeneration++; vm.modal=null;
await vm.supplementProductionCompletion(row);
await vm.submitProductionSupplement();
assert.equal(posts.length,1);
assert.strictEqual(vm.productionSupplementForm,oldForm);
assert.strictEqual(oldForm.payload,oldPayload);
assert.equal(vm.modal,null);
for (const nextActor of [41,42]) {
 const {vm,axios,posts}=await ready();
 let resolvePost;
 axios.post=(url,payload)=>{posts.push({url,payload});return new Promise(resolve=>{resolvePost=resolve;});};
 const pending=vm.submitProductionSupplement();
 const oldForm=vm.productionSupplementForm;
 const oldPayload=oldForm.payload;
 vm.user={id:nextActor}; vm.authGeneration++;
 const nextForm={actorId:nextActor,authGeneration:vm.authGeneration,saving:true,error:'新请求'};
 vm.productionSupplementForm=nextForm; vm.productionBusy=true;
 resolvePost({data:{}}); await pending;
 assert.equal(vm.reads,0);
 assert.equal(oldForm.submitted,false);
 assert.strictEqual(oldForm.payload,oldPayload);
 assert.strictEqual(vm.productionSupplementForm,nextForm);
 assert.equal(nextForm.saving,true);
 assert.equal(nextForm.error,'新请求');
 assert.equal(vm.productionBusy,true);
 assert.equal(vm.notices.length,0);
}
""",
    """
assert.equal(authWatcher.flush,'sync');
const {vm,axios,posts}=await ready();
let resolvePost;
axios.post=(url,payload)=>{posts.push({url,payload});return new Promise(resolve=>{resolvePost=resolve;});};
const pending=vm.submitProductionSupplement();
const oldForm=vm.productionSupplementForm;
const oldPayload=oldForm.payload;
assert.equal(oldForm.saving,true);
assert.equal(vm.productionBusy,true);
vm.expireSession(42);
assert.equal(oldForm.saving,false);
assert.equal(vm.productionBusy,false);
assert.strictEqual(oldForm.payload,oldPayload);
await vm.submitProductionSupplement();
assert.equal(posts.length,1);
vm.productionBusy=true;
const noticeCount=vm.notices.length;
resolvePost({data:{}});await pending;
assert.equal(vm.productionBusy,true);
assert.equal(oldForm.submitted,false);
assert.strictEqual(oldForm.payload,oldPayload);
assert.equal(vm.reads,0);
assert.equal(vm.notices.length,noticeCount);
const unrelated=setup().vm;
unrelated.productionBusy=true;
unrelated.expireSession(42);
assert.equal(unrelated.productionBusy,true);
""",
])
def test_supplement_form_behavior(tmp_path: Path, scenario: str) -> None:
    _run(tmp_path, scenario)


def test_supplement_modal_has_one_confirmation_and_readonly_retry() -> None:
    start = INDEX.index('<form v-else-if="modal.type === \'productionSupplement\'')
    end = INDEX.index('<div v-else-if="modal.type === \'productionLabelMaintenance\'">', start)
    form = INDEX[start:end]
    assert '@submit.prevent="submitProductionSupplement"' in form
    assert form.count('type="submit"') == 1
    assert 'v-model.number="productionSupplementForm.location_id"' in form
    assert "本次损耗" in form and "合格品全部进入" in form
    assert "!!productionSupplementForm.payload" in form
    assert "刷新列表" in form and "重试本次补录" in form

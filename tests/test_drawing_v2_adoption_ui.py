"""Run shipped Vue methods in Node; no browser, server or database is started."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def run_methods(program, *, editor=False):
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node unavailable; no dependency installation')
    source = Path('static/index.html').read_text(encoding='utf-8')
    helper = source[source.index('// BEGIN IDEMPOTENCY_KEY_HELPER'):source.index('// END IDEMPOTENCY_KEY_HELPER')]
    if editor:
        methods = source[source.index('          async changeDrawingV2Template()'):source.index('          async openProduct(row=null)')]
    else:
        methods = source[source.index('// BEGIN DRAWING_ADOPTION_METHODS'):source.index('// END DRAWING_ADOPTION_METHODS')]
    setup = r'''
const assert=require('node:assert/strict'), vm=require('node:vm');
const sandbox={axios:{}};
vm.runInNewContext(HELPER+'\nmethods=({'+METHODS+'});',sandbox);
function context(){return {...sandbox.methods,authGeneration:1,user:{id:5},canProductionExecute:true,
 productionBusy:false,productionDrawingAdoptionAttempts:{},productionPending:[{id:12,version:3,actual_input_quantity:75}],
 productionWaitingLabels:[],errorMessage(e){return e.message;},showToast(){},formatDateTime(v){return v;}};}
function data(task=12,version=3){return {task_id:task,task_version:version,current_release:null,candidates:[{id:7,number:'DWG-7',revision:'A'}],
 can_adopt:true,block_reason:null,confirmation_text:'尚未下发',history:[]};}
'''.replace('HELPER', json.dumps(helper)).replace('METHODS', json.dumps(methods))
    result = subprocess.run([node, '-e', setup+'\n(async()=>{'+program+'})().catch(e=>{console.error(e);process.exitCode=1;});'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr


def test_adoption_requires_explicit_choice_and_retries_identical_request_after_reopen():
    run_methods(r'''
const c=context(), requests=[];
sandbox.axios.get=async()=>({data:data()});
sandbox.axios.post=async(url,payload)=>{requests.push(JSON.stringify(payload));if(requests.length===1)throw Error('response lost');return {data:{task_version:4,replayed:true}};};
await c.openProductionDrawingAdoption({id:12,product_code:'UAT'});
assert.equal(c.productionDrawingAdoption.releaseId,null);assert.equal(c.productionDrawingAdoption.confirmed,false);
assert.equal(await c.saveProductionDrawingAdoption(),false);assert.equal(requests.length,0);
c.productionDrawingAdoption.releaseId=7;
assert.equal(await c.saveProductionDrawingAdoption(),false);assert.equal(requests.length,0);
c.productionDrawingAdoption.confirmed=true;
assert.equal(await c.saveProductionDrawingAdoption(),false);assert.equal(requests.length,1);
assert.ok(c.productionDrawingAdoptionAttempts[12]);
await c.openProductionDrawingAdoption({id:12});
assert.ok(c.productionDrawingAdoption.pending);
c.productionDrawingAdoption.releaseId=99; // Retry cannot mutate the original payload.
assert.equal(await c.saveProductionDrawingAdoption(),true);
assert.equal(requests[0],requests[1]);
const request=JSON.parse(requests[0]);assert.equal(request.release_id,7);assert.equal(request.expected_task_version,3);
assert.ok(request.idempotency_key.length>=12);assert.equal(request.confirmed_not_issued,true);
assert.equal(c.productionDrawingAdoptionAttempts[12],undefined);
assert.equal(c.productionPending[0].version,4);assert.equal(c.productionPending[0].actual_input_quantity,75);
''')


def test_adoption_version_conflict_retry_header_and_late_reads():
    run_methods(r'''
const c=context();let reads=0,mode='version';
sandbox.axios.get=async()=>{reads++;return {data:data()};};
sandbox.axios.post=async()=>{const e=Error('conflict');e.response={status:409,headers:mode==='retry'?{'x-drawing-adoption-retry':'same-request'}:{}};throw e;};
await c.openProductionDrawingAdoption({id:12});
c.productionDrawingAdoption.releaseId=7;c.productionDrawingAdoption.confirmed=true;
await c.saveProductionDrawingAdoption();assert.equal(reads,2);assert.equal(c.productionDrawingAdoption.releaseId,null);
assert.equal(c.productionDrawingAdoption.confirmed,false);assert.equal(c.productionDrawingAdoption.pending,null);
mode='retry';c.productionDrawingAdoption.releaseId=7;c.productionDrawingAdoption.confirmed=true;
await c.saveProductionDrawingAdoption();assert.ok(c.productionDrawingAdoption.pending);assert.equal(reads,2);
const pendingKey=c.productionDrawingAdoption.pending.payload.idempotency_key;
await c.saveProductionDrawingAdoption();assert.equal(c.productionDrawingAdoption.pending.payload.idempotency_key,pendingKey);
let resolve; sandbox.axios.get=()=>new Promise(r=>{resolve=r;});
const old=c.openProductionDrawingAdoption({id:20});
sandbox.axios.get=async()=>({data:data(21,6)});
await c.openProductionDrawingAdoption({id:21});resolve({data:data(20,5)});await old;
assert.equal(c.productionDrawingAdoption.taskId,21);assert.equal(c.productionDrawingAdoption.data.task_version,6);
c.canProductionExecute=false;c.productionDrawingAdoption.releaseId=7;c.productionDrawingAdoption.confirmed=true;
assert.equal(await c.saveProductionDrawingAdoption(),false);
''')


def test_slotted_suggestions_preserve_manual_values_and_disabled_flag_keeps_release_readable():
    run_methods(r'''
const c={...sandbox.methods,productForm:{id:42,version:1},
 drawingV2:{template_key:'slotted_v1',parameters:{panel_1_mm:'321.25'},print_objects:[],paper_color:'white',version:null},
 _productFormSaveFields(){return {id:42};},productFormSnapshot:'{"id":42}',errorMessage(e){return e.message;},showToast(){}};
let requests=0, queriedTemplate;
sandbox.axios.get=async(url,config)=>{requests++;queriedTemplate=config.params.template_key;return {data:{product_version:1,parameters:{panel_1_mm:'300',panel_2_mm:'200.5',slot_width_mm:null}}};};
await c.suggestDrawingV2Parameters();assert.equal(queriedTemplate,'slotted_v1');
assert.equal(c.drawingV2.parameters.panel_1_mm,'321.25');assert.equal(c.drawingV2.parameters.panel_2_mm,'200.5');assert.equal(c.drawingV2.parameters.slot_width_mm,null);
sandbox.axios.get=async()=>({data:{editing_enabled:false,draft:null,releases:[{id:2,number:'OLD',revision:'A'}]}});
await c.loadDrawingV2();assert.equal(c.drawingV2.editing_enabled,false);
assert.equal(c.drawingV2.releases[0].number,'OLD');
sandbox.axios.put=async()=>{throw Error('must not write');};sandbox.axios.post=sandbox.axios.put;
await c.saveDrawingV2();await c.publishDrawingV2();await c.suggestDrawingV2Parameters();
assert.equal(requests,1);
''', editor=True)

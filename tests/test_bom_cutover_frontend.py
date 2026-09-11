"""JS guards supplement (never replace) the required Chrome acceptance."""
import json
import subprocess
from pathlib import Path
import pytest


@pytest.mark.parametrize("rule_revision,route", [(None,"reserved-kit-cutover"), (0,"unstarted-bom-cutover"),
    (2,"unstarted-bom-cutover"), (0,"stocked-bom-cutover"), (2,"stocked-bom-cutover"), (0,"purchase-bom-cutover"), (2,"purchase-bom-cutover"), (0,"material-bom-cutover"), (2,"material-bom-cutover")])
def test_cutover_preview_and_uncertain_retry_use_same_payload(rule_revision,route):
    html = Path("static/index.html").read_text(encoding="utf-8")
    methods = html.split("          async openReservedKitCutover(", 1)[1].split("          async openOrderDetail(", 1)[0]
    methods = "async openReservedKitCutover(" + methods
    script = r'''
const assert=require('node:assert/strict');
const ctx=({METHODS});
let attempts=[], urls=[];
const review={ready:true,outputs:[{product_id:9}],target_locations:{9:20},reviewed_hash:'a'.repeat(64),
 preview_hash:'b'.repeat(64),source_lot_versions:{3:2},...(__RULE__===null?{}:{rule_revision:__RULE__})};
global.axios={post:async(url,payload)=>{urls.push(url);attempts.push(JSON.parse(JSON.stringify(payload)));
 if(url.endsWith('/execute')&&attempts.length===1)throw Error('network lost');return {data:review};}};
ctx.errorMessage=e=>e.message;ctx.showToast=()=>{};ctx.openOrderDetail=async()=>{};
ctx.modal={type:'bomCutover'};
ctx.bomCutover={itemId:1,orderId:2,route:__ROUTE__,review,confirmed:true,loading:false,pending:null};
(async()=>{
 await ctx.executeReservedKitCutover();
 assert.ok(ctx.bomCutover.pending);assert.equal(ctx.bomCutover.loading,false);
 const key=ctx.bomCutover.pending.operation_key;
 await ctx.executeReservedKitCutover();
 assert.equal(attempts[1].operation_key,key);assert.deepEqual(attempts[0],attempts[1]);
 assert.equal(urls[0],`/api/orders/items/1/${ctx.bomCutover.route}/execute`);assert.equal(urls[1],urls[0]);
 if(__RULE__!==null)assert.equal(attempts[0].rule_revision,__RULE__);
 else assert.equal(Object.hasOwn(attempts[0],'rule_revision'),false);
 assert.equal(ctx.bomCutover.pending,null);
 attempts=[];ctx.bomCutover.confirmed=false;await ctx.executeReservedKitCutover();assert.equal(attempts.length,0);
 ctx.bomCutover.targets={9:0};await ctx.previewReservedKitCutover();assert.equal(attempts.length,0);
 ctx.bomCutover.targets={9:20};await ctx.previewReservedKitCutover();assert.equal(ctx.bomCutover.confirmed,true);
 global.axios.post=async()=>{const e=Error('stale');e.response={status:409};throw e;};
 await ctx.executeReservedKitCutover();assert.equal(ctx.bomCutover.pending,null);assert.equal(ctx.bomCutover.confirmed,false);
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace("METHODS", methods).replace("__RULE__", json.dumps(rule_revision)).replace("__ROUTE__",json.dumps(route))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr

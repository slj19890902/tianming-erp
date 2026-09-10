"""JS guards supplement (never replace) the required Chrome acceptance."""
import json
import subprocess
from pathlib import Path


def test_cutover_preview_and_uncertain_retry_use_same_payload():
    html = Path("static/index.html").read_text(encoding="utf-8")
    methods = html.split("          async openReservedKitCutover(", 1)[1].split("          async openOrderDetail(", 1)[0]
    methods = "async openReservedKitCutover(" + methods
    script = r'''
const assert=require('node:assert/strict');
const ctx=({METHODS});
let attempts=[];
const review={ready:true,outputs:[{product_id:9}],target_locations:{9:20},reviewed_hash:'a'.repeat(64),
 preview_hash:'b'.repeat(64),source_lot_versions:{3:2}};
global.axios={post:async(url,payload)=>{attempts.push(JSON.parse(JSON.stringify(payload)));
 if(url.endsWith('/execute')&&attempts.length===1)throw Error('network lost');return {data:review};}};
ctx.errorMessage=e=>e.message;ctx.showToast=()=>{};ctx.openOrderDetail=async()=>{};
ctx.modal={type:'bomCutover'};
ctx.bomCutover={itemId:1,orderId:2,review,confirmed:true,loading:false,pending:null};
(async()=>{
 await ctx.executeReservedKitCutover();
 assert.ok(ctx.bomCutover.pending);assert.equal(ctx.bomCutover.loading,false);
 const key=ctx.bomCutover.pending.operation_key;
 await ctx.executeReservedKitCutover();
 assert.equal(attempts[1].operation_key,key);assert.deepEqual(attempts[0],attempts[1]);
 assert.equal(ctx.bomCutover.pending,null);
 attempts=[];ctx.bomCutover.confirmed=false;await ctx.executeReservedKitCutover();assert.equal(attempts.length,0);
 ctx.bomCutover.targets={9:0};await ctx.previewReservedKitCutover();assert.equal(attempts.length,0);
 ctx.bomCutover.targets={9:20};await ctx.previewReservedKitCutover();assert.equal(ctx.bomCutover.confirmed,true);
 global.axios.post=async()=>{const e=Error('stale');e.response={status:409};throw e;};
 await ctx.executeReservedKitCutover();assert.equal(ctx.bomCutover.pending,null);assert.equal(ctx.bomCutover.confirmed,false);
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace("METHODS", methods)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr

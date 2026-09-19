"""Retry state guards; not a substitute for Chrome page acceptance."""
from pathlib import Path
import subprocess
import pytest


@pytest.mark.parametrize('route',['semi-production','component-processing'])
def test_semi_confirmation_keeps_payload_after_uncertain_result(route):
    html = Path("static/index.html").read_text(encoding="utf-8")
    methods = "async openSemiProduction(" + html.split("          async openSemiProduction(", 1)[1].split("          async openReservedKitCutover(", 1)[0]
    script = r'''
const assert=require('node:assert/strict');
const ctx=({METHODS});
const attempts=[];
global.axios={post:async(url,payload)=>{assert.ok(url.includes('/ROUTE/'));attempts.push(structuredClone(payload));if(attempts.length===1)throw Error('network lost');return {data:{completion_id:1}}},get:async()=>({data:{products:[],completions:[]}})};
ctx.errorMessage=e=>e.message;ctx.showToast=()=>{};ctx.rememberModalOpener=()=>null;ctx.focusAccessibleModal=()=>{};
ctx.semiProduction={route:'ROUTE',itemId:1,productId:2,loading:false,pending:null,review:{ready:true,reviewed_hash:'a'.repeat(64)}};
(async()=>{
 await ctx.confirmSemiProduction();assert.ok(ctx.semiProduction.pending);
 const state=ctx.semiProduction,key=state.pending.operation_key;
 await ctx.openSemiProduction({id:7});assert.equal(ctx.semiProduction,state);assert.equal(state.itemId,1);
 await ctx.openSemiProduction({id:1});assert.equal(state.pending.operation_key,key);
 await ctx.confirmSemiProduction();assert.deepEqual(attempts[0],attempts[1]);assert.equal(state.pending,null);assert.equal(state.review,null);
 state.review={ready:true,reviewed_hash:'b'.repeat(64)};
 global.axios.post=async()=>{const error=Error('stale');error.response={status:409};throw error};
 await ctx.confirmSemiProduction();assert.equal(state.pending,null);assert.equal(state.review,null);
})().catch(error=>{console.error(error);process.exit(1)});
'''.replace("METHODS", methods).replace('ROUTE',route)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout+result.stderr

from pathlib import Path
import subprocess


def test_raw_purchase_retry_preserves_payload_and_ignores_stale_preview():
    html=Path('static/index.html').read_text(encoding='utf-8')
    methods='async openRawPurchase('+html.split('          async openRawPurchase(',1)[1].split('          async openSupplierRequisitionDraft(',1)[0]
    script=r'''
const assert=require('node:assert/strict');global.crypto=require('node:crypto').webcrypto;
const ctx=({METHODS});ctx.errorMessage=e=>e.message;ctx.rememberModalOpener=()=>null;ctx.focusAccessibleModal=()=>{};
ctx.loadRequisition=async()=>{throw Error('refresh failed')};
const form={quantity:45};const state={form,review:{reviewed_hash:'a'.repeat(64)},reviewPayload:structuredClone(form),pending:null,loading:false};ctx.rawPurchase=state;
const attempts=[];global.axios={post:async(u,p)=>{attempts.push(structuredClone(p));if(attempts.length===1)throw Error('timeout');return {data:{id:1}}}};
(async()=>{
 await ctx.confirmRawPurchase();assert.ok(state.pending);const key=state.pending.operation_key;
 await ctx.openRawPurchase(true);assert.equal(ctx.rawPurchase,state);assert.equal(state.pending.operation_key,key);
 await ctx.confirmRawPurchase();assert.deepEqual(attempts[0],attempts[1]);assert.equal(state.pending,null);assert.equal(state.result.id,1);
 await ctx.confirmRawPurchase();assert.equal(attempts.length,2);
 state.result=null;state.review=null;
 let release;global.axios.post=()=>new Promise(r=>release=r);
 const p=ctx.previewRawPurchase();state.form.quantity=46;release({data:{reviewed_hash:'b'.repeat(64)}});await p;
 assert.equal(state.review,null);assert.equal(state.loading,false);
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('METHODS',methods)
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stdout+result.stderr

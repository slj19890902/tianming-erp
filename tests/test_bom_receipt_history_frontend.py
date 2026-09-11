import subprocess

from tests.test_multilevel_bom_frontend import HTML, method


def test_reversal_cancel_retry_and_permissions_in_real_method():
    script = "const assert=require('node:assert/strict'); const methods={" + method('reverseExternalReceipt') + "};\n"
    script += """
let prompts=0, calls=[], fail=true, answer=null;
const prompt=()=>{prompts++;return answer;}, createIdempotencyKey=()=> 'same-key';
const axios={post:async(url,payload)=>{calls.push([url,JSON.stringify(payload)]);if(fail)throw new Error('timeout');}};
const ctx={...methods,user:{id:1,role:'admin'},authGeneration:1,activePage:'incoming',incomingWorkspace:'external-packaging',
 externalReceiptReverseBusy:null,externalReceiptReverseAttempts:{},externalReceiptHistoryPage:1,
 hasPermission:()=>true,showToast:()=>{},errorMessage:e=>e.message,invalidatePageCache:()=>{},
 loadExternalReceiptHistory:async()=>true,loadExternalIncoming:async()=>true};
(async()=>{
 const row={id:3,receipt_number:'ER-3',supports_reversal:true,reversed:false};
 assert.equal(await ctx.reverseExternalReceipt(row),false);assert.equal(calls.length,0);
 answer='误收';
 assert.equal(await ctx.reverseExternalReceipt(row),false);
 fail=false;assert.equal(await ctx.reverseExternalReceipt(row),true);
 assert.equal(prompts,2);assert.equal(calls.length,2);assert.equal(calls[0][1],calls[1][1]);
 assert.equal(row.reversed,true);assert.equal(ctx.externalReceiptReverseBusy,null);
 ctx.user.role='sales';await ctx.reverseExternalReceipt({...row,reversed:false});assert.equal(calls.length,2);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(['node'],input=script,text=True,capture_output=True,encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_history_request_uses_own_generation_and_renders_reversal_entry():
    source = method('loadExternalReceiptHistory')
    assert 'latestRequestControllers.get("incoming:bom-receipts")===controller' in source
    assert 'generation===this.authGeneration' in source
    assert 'page_size:25' in source
    assert '@click="reverseExternalReceipt(receipt)"' in HTML
    assert "receipt.supports_reversal && user?.role==='admin' && hasPermission('incoming.execute')" in HTML
    assert 'this.externalReceiptReverseAttempts = {}' in HTML


def test_late_history_response_does_not_cross_login_generation():
    script = "const assert=require('node:assert/strict'); const methods={" + method('loadExternalReceiptHistory') + "};\n"
    script += """
const latestRequestControllers=new Map();let resolve;
const axios={get:()=>new Promise(done=>{resolve=done})};
const ctx={...methods,user:{id:1},authGeneration:1,activePage:'incoming',incomingWorkspace:'external-packaging',
 externalReceiptHistory:[],externalReceiptHistoryQuery:'',
 beginLatestRequest:key=>{const c=new AbortController();latestRequestControllers.set(key,c);return c},
 finishLatestRequest:()=>{},isCancelledRequest:()=>false,errorMessage:String};
(async()=>{
 const first=ctx.loadExternalReceiptHistory();ctx.authGeneration++;
 resolve({data:{items:[{id:3}],total:1}});assert.equal(await first,false);
 assert.equal(ctx.externalReceiptHistory.length,0);
 const second=ctx.loadExternalReceiptHistory(2);resolve({data:{items:[{id:4}],total:26}});
 assert.equal(await second,true);assert.equal(ctx.externalReceiptHistory[0].id,4);
 assert.equal(ctx.externalReceiptHistoryPage,2);assert.equal(ctx.externalReceiptHistoryLoading,false);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(['node'],input=script,text=True,capture_output=True,encoding='utf-8')
    assert result.returncode == 0, result.stderr

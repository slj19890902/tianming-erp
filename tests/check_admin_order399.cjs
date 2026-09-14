const fs=require('fs'), vm=require('vm'), assert=require('assert');
vm.runInThisContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'));
const html=fs.readFileSync('static/index.html','utf8');
for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  if(!/\bsrc=|application\/json/i.test(match[1]) && match[2].trim()) new vm.Script(match[2]);
}
const source=html.slice(html.indexOf('async previewAdminOrderAction(mode)'),html.indexOf('async dangerRollbackOrderGroup()'));
let calls=[], closed=0, fail=false;
const axios={post:async(url,body)=>{
  calls.push([url,body]);
  if(url.endsWith('/preview')) return {data:{label:'删除试验单',mode:'delete_trial',order_ids:[1],reviewed_hash:'a'.repeat(64),blockers:[]}};
  if(fail) throw Error('network');
  return {data:{message:'已完成'}};
}};
const createIdempotencyKey=()=> 'test-ui-admin399';
const methods=eval('({'+source+'})');
const state=Vue.reactive({adminOrderAction:{busy:false},orderGroupSaveState:{saving:false},orderEditForm:{order_ids:[1]}});
Object.assign(state,methods,{errorMessage:e=>e.message,closeModal:()=>{closed++},showToast:()=>{}});
for(const key of ['loadOrders','loadRequisition','loadDeliveries','loadFinance','loadKpi']) state[key]=async()=>{};
(async()=>{
  let visible=null;
  Vue.effect(()=>visible=state.adminOrderAction.plan?.label);
  await state.previewAdminOrderAction('delete_trial');
  assert.equal(visible,'删除试验单');
  state.adminOrderAction.trialConfirmed=true;
  fail=true;await state.executeAdminOrderAction();
  assert.equal(closed,0);assert.equal(state.adminOrderAction.error,'network');
  assert.equal(state.orderGroupSaveState.saving,false);
  fail=false;await state.executeAdminOrderAction();
  assert.equal(closed,1);assert.deepEqual(calls[1][1],calls[2][1]);
  assert.equal(state.adminOrderAction.busy,false);
  console.log('PASS: inline JS syntax, reactive preview, single submit, uncertain retry retains exact request, refresh');
})().catch(e=>{console.error(e);process.exit(1)});

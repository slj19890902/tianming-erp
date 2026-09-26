const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const method=(start,end)=>new AsyncFunction('row',html.split(start)[1].split(end)[0].replace(/},\s*$/,''));
const open=method('async openStatementMerge(row) {','async saveStatementMerge() {');
const save=method('async saveStatementMerge() {','openStatementInvalidate(row) {');
const context=vm.createContext({console});
vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),context);
global.createIdempotencyKey=()=> 'merge-ui-key';
global.confirm=()=>true;
(async()=>{
  const ctx=context.Vue.reactive({authGeneration:1,statementMerge:{saving:false},modal:null,
    errorMessage:e=>e.message,showToast:message=>messages.push(message),
    openStatementDetail:async row=>{opened=row.id;},exportStatement:async()=>false,
    loadFinance:async()=>{throw new Error('refresh failed');}});
  let resolvers=[],messages=[],opened=null,posts=0,payloads=[];
  global.axios={get:()=>new Promise(resolve=>resolvers.push(resolve))};
  const first=open.call(ctx,{id:1}), second=open.call(ctx,{id:2});
  resolvers[1]({data:{items:[{id:2,version:1,ledger_version:1,blocker:'',total_receivable:'10'},
    {id:3,version:1,ledger_version:1,blocker:'',total_receivable:'20'}]}});
  assert.equal(await second,true);
  resolvers[0]({data:{items:[{id:1}]}});assert.equal(await first,false);
  assert.equal(ctx.statementMerge.items[0].id,2,'Older request must not replace another customer');
  ctx.statementMerge.selected[3]=true;
  let finish;
  global.axios.post=async (url,payload)=>{posts++;payloads.push(JSON.stringify(payload));await new Promise(resolve=>finish=resolve);throw new Error('network uncertain');};
  const pending=save.call(ctx);assert.equal(await save.call(ctx),false);assert.equal(posts,1);
  finish();assert.equal(await pending,false);
  global.axios.post=async(url,payload)=>{posts++;payloads.push(JSON.stringify(payload));return {data:{id:9}};};
  assert.equal(await save.call(ctx),true);assert.equal(payloads[0],payloads[1]);
  assert.equal(opened,9);assert(messages.some(m=>m.includes('合并已保存，导出失败')));
  assert(messages.some(m=>m.includes('合并已保存，列表刷新失败')));
  assert.equal(await save.call(ctx),true);assert.equal(posts,2,'Saved result must not be recreated');
  console.log('PASS merge reactive form, stale results, duplicate click, retry and saved-but-export/refresh failure');
  const scripts=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m=>m[1]).filter(s=>s.trim());
  scripts.forEach(s=>new vm.Script(s));
  console.log(`PASS complete inline JavaScript syntax (${scripts.length} scripts)`);
})().catch(error=>{console.error(error);process.exitCode=1;});

const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
for(const [index,match] of [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].entries()) {
  if(match[1].trim()) new vm.Script(match[1],{filename:`inline-${index}.js`});
}
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
function method(start,end){return html.split(start)[1].split(end)[0].replace(/},\s*$/, '');}
const open=new AsyncFunction('statementId',method('async openPartnerInvoiceMerge(statementId) {','async mergeAndDownloadPartnerInvoice() {'));
const merge=new AsyncFunction(method('async mergeAndDownloadPartnerInvoice() {','async downloadInvoiceTaxTemplate(task, mergedScopeChecked = false) {'));
(async()=>{
 const pending=[];
 global.axios={get:()=>new Promise(resolve=>pending.push(resolve))};
 const ctx={partnerInvoiceMerge:{requestToken:0},modal:null,showToast(){},errorMessage:e=>String(e)};
 const first=open.call(ctx,1),second=open.call(ctx,2);
 pending[1]({data:{scope_hash:'second',statements:[{statement_id:2}]}});await second;
 pending[0]({data:{scope_hash:'first',statements:[{statement_id:1}]}});await first;
 assert.equal(ctx.partnerInvoiceMerge.statementId,2);
 assert.equal(ctx.partnerInvoiceMerge.preview.scope_hash,'second');
 let posts=0,resolvePost;
 global.axios={post:()=>{posts++;return new Promise(resolve=>{resolvePost=resolve;});}};
 Object.assign(ctx,{partnerInvoiceMerge:{statementId:2,preview:{scope_hash:'same',blockers:[]},key:'same-key',saving:false},
  async downloadInvoiceTaxTemplate(task,checked){assert.equal(checked,true);return false;},
  async loadFinance(){},async loadInvoiceTasks(){},closeModal(){this.modal=null;}});
 const firstMerge=merge.call(ctx);
 assert.equal(await merge.call(ctx),false);
 resolvePost({data:{id:3}});await firstMerge;
 assert.equal(posts,1);assert.equal(ctx.partnerInvoiceMerge.saving,false);
 console.log('PASS complete inline JavaScript, partner request ordering and duplicate-click/download-failure recovery');
})().catch(error=>{console.error(error);process.exitCode=1;});

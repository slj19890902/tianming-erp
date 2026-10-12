const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[2]||'static/index.html','utf8');
function extract(name,box) {
 const m=new RegExp('^([\\t ]*)(?:async )?'+name+'\\(','m').exec(html);
 if(!m)return null;
 const closing='\n'+m[1]+'},',end=html.indexOf(closing,m.index);
 return vm.runInContext('({'+html.slice(m.index,end+closing.length)+'})',box)[name];
}
function setup() {
 const box=vm.createContext({axios:{}});
 const ctx={user:{id:1,role:'admin'},authGeneration:1,canCreateOrders:true,
  openOrderPdfImport(){this.modal={type:'orderPdfImport'};this.orderImportBatch={status:'idle'};this.orderImportDrafts=[];},
  prepareImportDraftReminderState:d=>d,preparePdfImportItem:i=>i,loadOrderImportReminders:async()=>{},
  loadOrderLineInventory:async()=>{},refreshEmailQueueCount:async()=>{},showToast(){},};
 for(const name of ['errorMessage','emailQueueRequestError','openEmailQueue']){const fn=extract(name,box);if(fn)ctx[name]=fn;}
 return {box,ctx};
}
(async()=>{
 let {box,ctx}=setup(),request;
 box.axios.post=async(...args)=>{request=args;throw {code:'ERR_NETWORK',message:'Network Error'};};
 await ctx.openEmailQueue();
 assert.match(ctx.emailQueueError,/邮箱收单.*连接失败/);
 assert.equal(ctx.orderImportBatch.status,'failed');
 assert.ok(request[2].timeout>0,'queue initialization must have a bounded timeout');
 ({box,ctx}=setup());
 box.axios.post=async()=>{throw {code:'ECONNABORTED',message:'timeout'};};
 await ctx.openEmailQueue();assert.match(ctx.emailQueueError,/超时/);
 ({box,ctx}=setup());
 box.axios.post=async()=>{throw {response:{status:403,data:{detail:'无邮箱查看权限'}},message:'HTTP403'};};
 await ctx.openEmailQueue();assert.equal(ctx.emailQueueError,'无邮箱查看权限');
 ({box,ctx}=setup());let reject;
 box.axios.post=()=>new Promise((resolve,r)=>{reject=r;});
 const old=ctx.openEmailQueue();ctx.modal={type:'orderPdfImport'};ctx.orderImportBatch={status:'new-session'};ctx.authGeneration++;
 reject({code:'ERR_NETWORK',message:'Network Error'});await old;
 assert.equal(ctx.orderImportBatch.status,'new-session','late failure must not mutate another modal/session');
 ({box,ctx}=setup());let resolve;
 box.axios.post=()=>new Promise(r=>{resolve=r;});
 const stale=ctx.openEmailQueue();ctx.user={id:2,role:'admin'};ctx.authGeneration++;
 resolve({data:{drafts:[{items:[]}]}});await stale;
 assert.equal(ctx.orderImportDrafts.length,0,'late response must not display previous actor drafts');
 console.log('queue network/timeout/permission errors and late session results passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

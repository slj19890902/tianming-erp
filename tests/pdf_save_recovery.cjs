const assert = require('node:assert/strict');
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2] || 'static/index.html','utf8');
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).find(x=>x.trim());
const storage = new Map();
const box = {axios:{defaults:{},interceptors:{response:{use(){}}}},Vue:{createApp(d){box.methods=d.methods;return {component(){return this},mount(){return this}}}},
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},window:{},TMOrderReference:{component:{}},console,URLSearchParams,setTimeout,clearTimeout};
vm.createContext(box);vm.runInContext(source,box);
function draft(name='one',po='PO1'){return {source_name:name,file_hash:name,order_date:'2026-09-19',confirmed:true,matched_customer_id:1,customer_po:po,preview_safety_token:'signed',integrity_check:{integrity_status:'passed'},items:[{matched_product_id:1,quantity:10,unit_price:1}],_save_status:'idle'};}
let refreshes=0, posts=[], records=new Map(), lost=true, reject=false;
box.axios.get=async url=>({data: records.has(url.split('/').pop())?{status:'completed',order:records.get(url.split('/').pop())}:{status:'not_found'}});
box.axios.post=async (url,p)=>{assert.equal(url,'/api/orders');assert.ok(p.idempotency_key,'PDF must send stable idempotency key');assert.ok(p.items[0].client_line_id);posts.push(JSON.stringify(p));if(reject)throw {response:{status:422,data:{detail:'invalid'}}};let data=records.get(p.idempotency_key);if(!data){data={id:records.size+1,customer_id:1};records.set(p.idempotency_key,data);}if(lost){lost=false;throw Error('response lost');}return {data};};
function context(d){return {...box.methods,user:{id:7},orderImportDrafts:d,orderImportBatch:{retryDraft:null},canConfirmImportDraft:()=>true,
 prepareMoldRepairConfirmation:async()=>({confirmed:true,confirmation_token:null}),refreshImportDraftInventoryForSave:async()=>{refreshes++},
 refreshPdfPriceConflict(){},inventoryDecisionRequired:()=>'',pdfImportItemMaterialCode:()=>'',buildReservationPlan:()=>({finished:[],semi:[]}),
 loadOrders:async()=>{},loadKpi:async()=>{},showToast(){},showOrderNextStepGuide(){},refreshEmailQueueCount(){},normalizeOrderSaveError:()=> 'error'};}
(async()=>{
 for(const po of ['PO1','']){
  records.clear();posts=[];refreshes=0;lost=true;
  const d=draft('file'+po,po), c=context([d]);await c.saveConfirmedImportDrafts();
  assert.equal(posts.length,1,'PDF request must include a stable key before submission');assert.equal(d._save_status,'unknown');assert(c.isImportDraftLocked(d));
  const frozen=posts[0];d.items[0].quantity=99;await c.retryFailedImportDraft(d);
  assert.equal(d._save_status,'success');assert.equal(d._saved_order_id,1);assert.equal(records.size,1);assert.equal(refreshes,1);
  assert.equal(posts[0],frozen);
 }
 // Lost response before commit: retry sends byte-equivalent original payload.
 records.clear();posts=[];lost=true;const d=draft('uncommitted'),c=context([d]);await c.saveConfirmedImportDrafts();records.clear();d.items[0].quantity=55;await c.retryFailedImportDraft(d);assert.equal(posts[0],posts[1]);
 // Reload resolves only; never posts an old token from storage.
 const restored=draft('uncommitted'),r=context([restored]);r.restorePdfSaveAttempt(restored);assert.equal(restored._save_status,'unknown');const n=posts.length;await r.retryFailedImportDraft(restored);assert.equal(restored._save_status,'success');assert.equal(posts.length,n);
 const absent=draft('absent'),a=context([absent]);lost=true;await a.saveConfirmedImportDrafts();records.clear();const fresh=draft('absent'),f=context([fresh]);f.restorePdfSaveAttempt(fresh);const before=posts.length;await f.retryFailedImportDraft(fresh);assert.equal(posts.length,before);assert.equal(fresh.confirmed,false);assert.equal(fresh._save_status,'idle');
 // Batch retains successful identity and only failed draft is retried.
 records.clear();lost=false;posts=[];const first=draft('batch1'),second=draft('batch2'),b=context([first,second]);const post=box.axios.post;box.axios.post=async(u,p)=>{reject=p.remark.includes('batch2');return post(u,p)};await b.saveConfirmedImportDrafts();assert.equal(first._save_status,'success');assert.equal(second._save_status,'failed');const key=second._create_key;box.axios.post=post;reject=false;second.confirmed=true;await b.saveConfirmedImportDrafts();assert.equal(records.size,2);assert.equal(second._create_key,key);assert.equal(posts.filter(x=>JSON.parse(x).remark.includes('batch1')).length,1);
 // Account separation and inaccessible storage fail closed before network write.
 const other=draft('batch1'),o=context([other]);o.user={id:8};o.restorePdfSaveAttempt(other);assert.equal(other._save_status,'idle');
 const switched=draft('batch1'),sw=context([switched]);sw.restorePdfSaveAttempt(switched);sw.user={id:8};const countBefore=posts.length;await sw.retryFailedImportDraft(switched);assert.equal(switched._save_status,'unknown');assert.equal(posts.length,countBefore);
 const deleted=draft('batch1'),dc=context([deleted]);dc.restorePdfSaveAttempt(deleted);box.axios.get=async()=>{throw {response:{status:409,data:{detail:'original deleted'}}}};await dc.retryFailedImportDraft(deleted);assert.equal(deleted._save_status,'unknown');assert.equal(posts.length,countBefore);
 const blocked=draft('storage'),s=context([blocked]);const count=posts.length;box.localStorage.setItem=()=>{throw Error('quota')};await s.saveConfirmedImportDrafts();assert.equal(posts.length,count);assert.equal(blocked._save_status,'failed');
 console.log('PASS: PDF loss/retry, frozen request, reload, partial batch, account and storage protection');
})().catch(e=>{console.error(e);process.exitCode=1});




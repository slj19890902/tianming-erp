const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(process.argv[2], 'static/index.html'), 'utf8');
const start = source.indexOf('async loadCustomerOptions(force=false) {');
const end = source.indexOf('async loadProducts() {', start);
const latestRequestControllers = new Map();
const load = eval('(' + source.slice(start,end).trim().replace(/,$/,'').replace(/^async loadCustomerOptions\(/,'async function loadCustomerOptions(') + ')');
function state() { return {
 user:{id:1}, authGeneration:1, customerOptions:[], customerOptionsIdentity:'1:1',
 hasPermission:()=>true,
 beginLatestRequest(key) { latestRequestControllers.get(key)?.abort(); const c=new AbortController();latestRequestControllers.set(key,c);return c; },
 finishLatestRequest(key,c) { if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key); },
 isCancelledRequest:error=>error.name==='AbortError',
}; }
function page(number) { return {data:{page:number,total:201,total_pages:2,items:number===1?Array.from({length:200},(_,i)=>({id:i+1,name:'Customer '+(i+1)})):[{id:201,name:'Last customer'}]}}; }
function deferred() { let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve}; }
let get;
global.axios={get:(...args)=>get(...args)};
(async()=>{
 const calls=[];
 get=async(url,{params})=>{assert.equal(url,'/api/master/customers');assert.equal(params.page_size,200);assert.equal(params.include_inactive,true);calls.push(params.page||1);return page(params.page||1);};
 const vm=state();await load.call(vm);
 assert.equal(vm.customerOptions.length,201,'customer 201 must remain selectable');assert.deepEqual(calls,[1,2]);
 await load.call(vm);assert.deepEqual(calls,[1,2],'same-session complete cache is reusable');
 const failed=state();failed.customerOptions=[{id:999,name:'complete cache'}];
 get=async(_,{params})=>{if(params.page===2)throw new Error('page two failed');return page(1);};
 await assert.rejects(()=>load.call(failed,true),/page two failed/);
 assert.deepEqual(failed.customerOptions,[{id:999,name:'complete cache'}]);
 const rejected=state();rejected.customerOptions=[{id:999,name:'revoked cache'}];
 get=async()=>{throw Object.assign(new Error('forbidden'),{response:{status:403}});};
 await assert.rejects(()=>load.call(rejected,true),/forbidden/);assert.deepEqual(rejected.customerOptions,[]);
 const cancelled=state();get=async()=>{throw Object.assign(new Error('cancelled'),{name:'AbortError'});};
 assert.equal(await load.call(cancelled,true),false);assert.deepEqual(cancelled.customerOptions,[]);
 const denied=state();denied.hasPermission=()=>false;get=async()=>{throw new Error('unauthorized request');};await load.call(denied);
 const pending=deferred();const revoked=state();get=()=>pending.promise;
 const obsolete=load.call(revoked,true);revoked.authGeneration=2;revoked.user={id:2};revoked.customerOptions=[{id:900,name:'new session'}];pending.resolve(page(1));await obsolete;
 assert.deepEqual(revoked.customerOptions,[{id:900,name:'new session'}],'old response cannot publish into a new session');
 const permission=deferred();const changed=state();get=()=>permission.promise;const changing=load.call(changed,true);changed.hasPermission=()=>false;permission.resolve(page(1));await changing;
 assert.deepEqual(changed.customerOptions,[],'revoked permission cannot publish any page');
 const waiting=deferred();const replaced=state();get=()=>waiting.promise;const old=load.call(replaced,true);
 get=async(_,{params})=>({data:{page:params.page,total:1,total_pages:1,items:[{id:700,name:'newer request'}]}});
 await load.call(replaced,true);waiting.resolve(page(1));await old;
 assert.deepEqual(replaced.customerOptions,[{id:700,name:'newer request'}]);
 const previousUser=state();previousUser.customerOptionsIdentity='9:9';previousUser.customerOptions=[{id:999,name:'previous user'}];
 get=async(_,{params})=>({data:{page:params.page,total:0,total_pages:0,items:[]}});await load.call(previousUser);
 assert.deepEqual(previousUser.customerOptions,[],'another identity must not reuse the warm cache');
 console.log('PASS: pagination, complete cache, failures, permissions, session changes and stale responses');
})().catch(error=>{console.error(error);process.exitCode=1;});

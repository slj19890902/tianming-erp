const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const method=(arg,start,end)=>new AsyncFunction(arg,html.split(start)[1].split(end)[0].replace(/},\s*$/,''));
const open=method('statementId','async openCustomerInvoiceMerge(statementId) {','async saveCustomerInvoiceMerge(download) {');
const save=method('download','async saveCustomerInvoiceMerge(download) {','async openPartnerInvoiceMerge(statementId) {');
const vue=vm.createContext({console});vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),vue);
global.createIdempotencyKey=()=> 'customer-merge-ui-key';
(async()=>{
 const ctx=vue.Vue.reactive({authGeneration:1,customerInvoiceMerge:{saving:false},modal:null,errorMessage:e=>e.message,showToast:()=>{},loadFinance:async()=>true,loadInvoiceTasks:async()=>true,downloadInvoiceTaxTemplate:async()=>false});
 let resolvers=[],posts=0,payloads=[];
 global.axios={get:()=>new Promise(resolve=>resolvers.push(resolve))};
 const first=open.call(ctx,1),second=open.call(ctx,2);
 resolvers[1]({data:{customer_name:'客户二',items:[{statement_id:2,blocker:''},{statement_id:3,blocker:''}]}});assert.equal(await second,true);
 resolvers[0]({data:{customer_name:'旧客户',items:[]}});assert.equal(await first,false);
 assert.equal(ctx.customerInvoiceMerge.anchorId,2);
 ctx.customerInvoiceMerge.selected[3]=true;
 global.axios.post=async(url,payload)=>{
   if(url.endsWith('preview')) return {data:{scope_hash:'a'.repeat(64),blockers:[],statements:[{},{}]}};
   posts++;payloads.push(JSON.stringify(payload));throw new Error('unknown network result');
 };
 assert.equal(await save.call(ctx,true),false);
 global.axios.post=async(url,payload)=>{posts++;payloads.push(JSON.stringify(payload));return {data:{id:9,customer_merge:true}};};
 assert.equal(await save.call(ctx,true),false);assert.equal(payloads[0],payloads[1]);
 assert(ctx.customerInvoiceMerge.error.includes('已保存'));
 assert.equal(await save.call(ctx,false),true);assert.equal(posts,2,'Saved merge must not be created twice');assert.equal(ctx.modal,null);
 const scripts=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m=>m[1]).filter(s=>s.trim());scripts.forEach(s=>new vm.Script(s));
 console.log('PASS customer merge: stale response isolation, uncertain retry same payload, retained save on export failure, no duplicate merge, inline JS syntax');
})().catch(e=>{console.error(e);process.exitCode=1;});

const formatMoney=new Function('value',html.split('money(value) {')[1].split('},')[0]);
const formatPrice=new Function('value',html.split('unitPrice(value) {')[1].split('},')[0]);
assert.equal(formatMoney('481.005'),'481.01');assert.equal(formatMoney('68.475'),'68.48');
assert.equal(formatPrice('1.2345'),'1.235');assert.equal(formatMoney('100537.3656'),'100537.37');
console.log('PASS Excel-compatible decimal display without binary toFixed ties');

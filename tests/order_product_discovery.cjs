const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('assert/strict');
const root=process.argv[2],html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const scripts=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).filter(x=>x.trim());
const pending=[];
const box={axios:{defaults:{},interceptors:{response:{use(){}}},get(url,options){return new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))}},
 Vue:{createApp(d){box.d=d;return {component(){return this},mount(){return this}}}},window:{},document:{},
 localStorage:{getItem(){return ''},setItem(){},removeItem(){}},TMOrderReference:{component:{}},URLSearchParams,setTimeout,clearTimeout,console};
vm.createContext(box);vm.runInContext(scripts[0],box);
const ctx={...box.d.methods,user:{id:1},authGeneration:1,orderForm:{customer_id:9},errorMessage:e=>e.message};
const answer=(request,items)=>request.resolve({data:{items,total:71,page:1,total_pages:2}});
(async()=>{
 ctx.resetOrderCommonBoxPicker();assert.equal(ctx.orderCommonBoxPicker.sort_by,'newest');
 ctx.orderCommonBoxPicker.selected={'7':{product:{id:7},quantity:12}};
 const first=ctx.loadOrderCommonBoxes();const request=pending.shift();
 assert.equal(request.options.params.sort_by,'newest');assert.equal(request.options.params.customer_id,9);
 assert.equal(request.options.params.selection_context,'order');
 answer(request,[{id:10}]);await first;assert.equal(ctx.orderCommonBoxPicker.items[0].id,10);
 ctx.orderCommonBoxPicker.filters={product_code:'old',product_name:'old',spec:'old'};
 const clear=ctx.clearOrderCommonBoxFilters(),clearRequest=pending.shift();
 assert.equal(clearRequest.options.params.product_code,'');assert.equal(clearRequest.options.params.page,1);
 answer(clearRequest,[{id:7}]);await clear;
 assert.equal(ctx.orderCommonBoxPicker.selected['7'].quantity,12);
 const old=ctx.loadOrderCommonBoxes(),olderRequest=pending.shift();
 ctx.orderCommonBoxPicker.sort_by='code';const latest=ctx.loadOrderCommonBoxes(2),newRequest=pending.shift();
 assert.equal(newRequest.options.params.sort_by,'code');assert.equal(newRequest.options.params.page,2);
 answer(newRequest,[{id:20}]);await latest;answer(olderRequest,[{id:1}]);await old;
 assert.equal(ctx.orderCommonBoxPicker.items[0].id,20);
 const other=ctx.loadOrderCommonBoxes(),otherRequest=pending.shift();ctx.orderForm.customer_id=99;
 answer(otherRequest,[{id:9}]);await other;assert.equal(ctx.orderCommonBoxPicker.items[0].id,20);
 const stale=ctx.loadOrderCommonBoxes(),staleRequest=pending.shift();ctx.authGeneration++;
 staleRequest.reject(new Error('previous user'));await stale;assert.equal(ctx.orderCommonBoxPicker.error,'');
 const closed=ctx.loadOrderCommonBoxes(),closedRequest=pending.shift();ctx.resetOrderCommonBoxPicker();
 answer(closedRequest,[{id:99}]);await closed;assert.equal(ctx.orderCommonBoxPicker.items.length,0);
 assert.ok(html.includes('最近新增优先'));assert.ok(html.includes('@click="clearOrderCommonBoxFilters"'));
 console.log('PASS newest/code requests, filter reset, retained selection, paging, stale/customer/account/closed responses');
})().catch(e=>{console.error(e);process.exitCode=1});

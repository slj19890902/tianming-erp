const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.trim());
const sandbox={axios:{defaults:{},interceptors:{request:{use(){}},response:{use(){}}}},
 Vue:{createApp(def){sandbox.methods=def.methods;return {};}},window:{},
 localStorage:{getItem(){return '';}},console,URLSearchParams,setTimeout,clearTimeout};
vm.createContext(sandbox);vm.runInContext(script.split('app.mixin(TMDemandStock.mixin)')[0],sandbox);
const methods=sandbox.methods;
const demand=require('../static/ui/demand-stock.js');
(async()=>{
 const existing={product_id:7,quantity:30};
 const ctx={...methods,orderForm:{items:[existing]},orderProductOptions:{},
   orderCommonBoxPicker:{items:[{id:8}],selected:{7:{product:{id:7},quantity:99},8:{product:{id:8},quantity:10}},importing:false},
   showToast(message){this.message=message;},reallocateAllDraftInventory(){},async refreshOrderNumberPreview(){}};
 assert.deepEqual(demand.mixin.methods.commonBoxDemandLines.call(ctx),[{product_id:8,quantity:10}],
   'picker must only query current search results');
 ctx.orderCommonBoxPicker.selected={7:{product:{id:7},quantity:99}};
 await ctx.importSelectedOrderCommonBoxes();
 assert.equal(existing.quantity,30,'duplicate import silently replaced delivery quantity');
 assert.match(ctx.message,/第1行/);
 assert.equal(ctx.moldLocationText({mold_display_name:'80012149 商品名称',mold_location:'A5-2'}),'A5-2');
 assert.equal(ctx.demandStockFigure([{client_line_id:'0',status:'empty',available_customer:0,unit:'个'}],0,'available_customer',false,''),'0 个');
 assert.equal(ctx.demandStockFigure([{client_line_id:'0',status:'empty',available_customer:0}],0,'available_customer',false,'network'),'查询失败');
 console.log('picker scope, duplicate quantity preservation and mold position passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const html=fs.readFileSync(new URL('../../../static/index.html',import.meta.url),'utf8');
const start=html.indexOf('          async stockPrepAction(');
const end=html.indexOf('          async loadProduction()',start);
const method=html.slice(start,end).trim().replace(/,$/,'');
test('uncertain POST retries the same key; changed quantity uses a new key',async()=>{
 const calls=[];
 const ctx=vm.runInNewContext(`({stockPrepBusy:false,productionLocations:[],${method}})`,{axios:{post:async(url,body)=>{calls.push({...body});throw new Error('network');}},window:{confirm:()=>true}});
 const row={receipt_item_id:386,lot_version:1,available:20,_quantity:5};
 await ctx.stockPrepAction(row,'plan');await ctx.stockPrepAction(row,'plan');
 assert.equal(calls.length,2);assert.equal(calls[0].operation_key,calls[1].operation_key);
 assert.ok(ctx.stockPrepError);row._quantity=6;await ctx.stockPrepAction(row,'plan');
 assert.notEqual(calls[1].operation_key,calls[2].operation_key);
});
test('invalid quantities, missing actual location and cancelled confirmation do not POST',async()=>{
 let calls=0;
 const ctx=vm.runInNewContext(`({stockPrepBusy:false,productionLocations:[],${method}})`,{axios:{post:async()=>{calls++;}},window:{confirm:()=>false}});
 const row={receipt_item_id:386,lot_version:1,available:20,_quantity:21};
 await ctx.stockPrepAction(row,'plan');
 await ctx.stockPrepAction(row,'complete',{id:1,version:1,_actual:5,_location:null});
 await ctx.stockPrepAction(row,'cancel',{id:1,version:1});assert.equal(calls,0);
});

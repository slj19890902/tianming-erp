import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const scan=fs.readFileSync(new URL('../../../static/shelf-scan.js',import.meta.url),'utf8');
const stock=fs.readFileSync(new URL('../../../static/mobile_stocktake.html',import.meta.url),'utf8');
test('scanner only refreshes on entry/manual/login/navigation, never periodically',()=>{
 assert.ok(!scan.includes('setInterval('));assert.ok(!scan.includes('visibilitychange'));
 assert.ok(scan.includes("'pageshow'"));assert.ok(scan.includes('event.persisted'));
 assert.ok(scan.includes('location_id=${encodeURIComponent(id)}&return_scan=1'));
});
test('stocktake returns to whole scanned location with no external redirect and blocks in-flight writes',()=>{
 const source=stock.slice(stock.indexOf('function restoreWarehouseAreaReturn()'),stock.indexOf('async function init()'));
 for(const [id,expected] of [['1884','/q/1884'],['https://evil.test',undefined],['0',undefined]]){
  const link={addEventListener:()=>{}},button={addEventListener:(name,fn)=>button.back=fn};let assigned;
  const ctx={URLSearchParams,window:{location:{search:'?return_scan=1&location_id='+encodeURIComponent(id),assign:value=>assigned=value}},document:{querySelector:()=>link},$:()=>button,state:{submitting:false},inbound:{busy:false,attempt:null}};
  vm.createContext(ctx);vm.runInContext(source,ctx);ctx.restoreWarehouseAreaReturn();assert.equal(link.href,expected);
  if(expected){ctx.state.submitting=true;button.back({preventDefault(){},stopImmediatePropagation(){}});assert.equal(assigned,undefined);ctx.state.submitting=false;button.back({preventDefault(){},stopImmediatePropagation(){}});assert.equal(assigned,expected);}
 }
});

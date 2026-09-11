import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../../../static/mobile_initial_stocktake_runtime.js',import.meta.url),'utf8');

test('mobile all customer lookup omits customer filter and keeps ranked candidate identity',async()=>{
  const elements={inboundCustomer:{value:'all'},inboundProduct:{innerHTML:''},inboundProductQuery:{value:'800×600'},inboundCandidates:{replaceChildren(){}},inboundContext:{}};
  let path;
  const context=vm.createContext({$:(id)=>elements[id],inbound:{generation:1},invalidateInboundSelection(){},showMessage(){},h:String,URLSearchParams,
    api:async(url)=>{path=url;return {items:[{product_id:9,customer_id:4,customer_short_name:'甲',product_code:'P9',product_name:'纸箱',specification:'800×600',match_score:100}]};}});
  const start=source.indexOf('async function findErpProducts()');const end=source.indexOf('\nasync function refreshInboundContext',start);
  vm.runInContext(source.slice(start,end),context);
  await vm.runInContext('findErpProducts()',context);
  assert.equal(new URL(path,'http://test').searchParams.has('customer_id'),false);
  assert.match(elements.inboundProduct.innerHTML,/data-customer-id="4"/);
  assert.match(elements.inboundProduct.innerHTML,/接近度100%/);
});

test('switching between cross-customer candidates always binds the chosen real customer',()=>{
  const elements={inboundProduct:{selectedOptions:[{dataset:{customerId:'4',customerName:'甲'}}]},inboundCustomer:{value:'all',append(){}}};
  const seen=[];
  const context=vm.createContext({$:(id)=>elements[id],document:{createElement:()=>({})},refreshInboundContext:()=>seen.push(elements.inboundCustomer.value)});
  const start=source.indexOf('$("inboundProduct").onchange =');const end=source.indexOf('$("inboundRefresh")',start);
  vm.runInContext(source.slice(start,end),context);
  elements.inboundProduct.onchange();
  elements.inboundProduct.selectedOptions[0].dataset={customerId:'7',customerName:'乙'};
  elements.inboundProduct.onchange();
  assert.deepEqual(seen,['4','7']);
});

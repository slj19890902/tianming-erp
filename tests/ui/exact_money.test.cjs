const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const money=require('../../static/ui/exact-money.js');
test('exact line and whole-order calculations match original decimal inputs',()=>{
 assert.equal(money.multiply('3','2.225'),'6.675');
 assert.equal(money.multiply('7','1.234567'),'8.641969');
 assert.equal(money.multiply('3','0.1'),'0.3');
 assert.equal(money.sum(['6.675','6.675']),'13.35');
 assert.equal(money.sum(['123456789.1234567891','.0000000001']),'123456789.1234567892');
 assert.equal(money.multiply('658','1.13'),'743.54');
 for(const value of ['NaN','Infinity','1e80','1e-50'])assert.equal(money.format(value),'—');
 assert.equal(money.format('-0.0004'),'-0.0004');
 assert.equal(money.format('3.290000'),'3.29');
});
test('all old order entry and edit fields retain original price and HTML compiles',()=>{
 const html=fs.readFileSync(path.join(__dirname,'../../static/index.html'),'utf8');
 for(const source of ['product.sale_unit_price','data.sale_unit_price','itemSnapshot.unit_price']){
  assert.equal(html.includes('this.unitPrice('+source+')'),false);
  assert.ok(html.includes('String('+source+' ?? "")'));
 }
 for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))if(match[1].trim())assert.doesNotThrow(()=>new vm.Script(match[1]));
});

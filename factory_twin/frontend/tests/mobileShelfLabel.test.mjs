import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../../../static/shelf-label.js',import.meta.url),'utf8');
test('position and selected-product labels are separate and contain no stock numbers',()=>{
  const h=value=>String(value??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  const ctx={h}; vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('function label('),source.indexOf('async function load(')),ctx);
  const row={title:'三楼 北货架G1',position:'02层-01格',qr_data_url:'QR'};
  const empty=ctx.label(row);
  assert.ok(empty.includes('class="location"'));
  for(const field of ['客户：','存货编码：','产品名称：','规格：'])assert.ok(!empty.includes(field));
  const product=ctx.label({...row,product:{customer:'甲客户',code:'SKU-1',name:'纸箱<script>',specification:'400×300×200',quantity:99999}});
  for(const field of ['甲客户','SKU-1','400×300×200','QR'])assert.ok(product.includes(field));
  assert.ok(!product.includes('99999'));assert.ok(!product.includes('<script>'));
});

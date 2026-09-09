import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../../../static/shelf-scan.js',import.meta.url),'utf8');
const ctx={};vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('const h ='),source.indexOf('let generation')),ctx);
test('scan uses Chinese units without changing quantities and escapes all product fields',()=>{
 for(const [input,expected] of [['boxes','箱'],[' BOXES ','箱'],['pcs','只'],['sets','套'],['张','张']])assert.equal(vm.runInContext(`unit(${JSON.stringify(input)})`,ctx),expected);
 const card=ctx.productCard({customer:'甲客户',customer_name:'甲客户有限公司<script>',code:'Z.001.000138',name:'纸箱',specification:'440×440×530',unit:'boxes',quantity:1000,available:900,reserved:100,lots:[]});
 assert.ok(card.includes('product-main'));assert.ok(card.includes('product-side'));
 assert.ok(card.includes('1000 <span>箱</span>'));assert.ok(!card.includes('boxes'));
 assert.ok(card.includes('Z.001.000138'));assert.ok(card.includes('甲客户有限公司&lt;script&gt;'));assert.ok(!card.includes('<script>'));
});
test('scan opts into the existing 30-day session and never stores the password',()=>{
 assert.ok(source.includes("remember_me:$('remember').checked"));
 assert.ok(!source.includes('localStorage'));assert.ok(!source.includes('sessionStorage'));
 const html=fs.readFileSync(new URL('../../../static/shelf-scan.html',import.meta.url),'utf8');
 assert.ok(html.includes('id="remember" type="checkbox" checked'));
 assert.ok(html.includes('公用手机勿选'));
});

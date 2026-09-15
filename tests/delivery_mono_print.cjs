const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const page=fs.readFileSync('static/delivery-print.html','utf8');
for(const script of page.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(script[1]);
const ctx=vm.createContext({getComputedStyle:()=>({paddingLeft:'3px',paddingRight:'3px'})});
vm.runInContext(page.slice(page.indexOf('    function orderNumberFontSize'),page.indexOf('    function renderDelivery')),ctx);
assert.equal(ctx.orderNumberFontSize(100,155),13);
assert.equal(ctx.orderNumberFontSize(300,150),6.5);
assert.equal(ctx.orderNumberFontSize(0,0),13);
for(const code of ['THPO202609070035','PO-<安全字符>-0001','PO-'+ '1234567890'.repeat(20)]){
  const text={textContent:code,style:{},parentElement:{clientWidth:162},getBoundingClientRect(){return {width:code.length*7*(parseFloat(this.style.fontSize)/13)};}};
  const root={querySelectorAll:()=>[text]};ctx.fitCustomerOrderNumbers(root);
  assert.equal(text.textContent,code);assert(text.getBoundingClientRect().width<=155);
  const initial=text.style.fontSize;ctx.fitCustomerOrderNumbers(root);assert.equal(text.style.fontSize,initial);
  if(code==='THPO202609070035')assert.equal(text.style.fontSize,'13px');
}
const css=page.slice(page.indexOf('<style>'),page.indexOf('</style>'));
assert(!css.includes('#dcfce7'));assert(!page.includes('TMOrderReference'));
assert.match(page,/fitCustomerOrderNumbers\(probe\)/);
assert.match(page,/beforeprint/);
assert.match(page,/escapeHtml\(String\(item.customer_po/);
assert.match(page,/class="item-quantity"/);
console.log('Inline syntax, single-line size fitting, sample PO, 203-character PO, repeat fitting, escaping and monochrome scope passed');

const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'../..');
function moduleFixture(){
 const context={};vm.createContext(context);
 vm.runInContext(fs.readFileSync(path.join(root,'static/ui/exact-money.js'),'utf8'),context);
 vm.runInContext(fs.readFileSync(path.join(root,'static/ui/finance-workspace.js'),'utf8'),context);
 return context.ERPFinanceWorkspace;
}
test('financial display keeps all significant decimal places and normalizes zero',()=>{
 const mod=moduleFixture();
 for(const [value,expected] of [['222.525','222.525'],['222.5','222.5'],['222.000','222'],[0,'0'],[-0.0004,'-0.0004'],[-2.5,'-2.5'],[1.2345,'1.2345'],[-1.2345,'-1.2345'],[222.9996,'222.9996']])assert.equal(mod.formatNumber(value),expected,String(value));
 let mixin;mod.install({mixin(value){mixin=value;},component(){}});
 assert.equal(mixin.methods.financeNumber('222.525'),'222.525');
});
test('financial templates and customer comparison use the same formatter without changing other pages',()=>{
 const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
 const details=html.slice(html.indexOf(`modal.type === 'statementDetail'`),html.indexOf(`modal.type === 'statementEdit'`));
 assert.ok(details.includes('financeNumber(item.receivable_amount)'));
 assert.ok(details.includes('financeNumber(item.unit_price_snapshot)'));
 assert.ok(details.includes('financeNumber(financeSettlementForm.statement.total_receivable)'));
 assert.equal(/\b(?:money|unitPrice)\(/.test(details),false);
 assert.ok(html.includes('money(value) { return Number(value || 0).toLocaleString'));
 const comparison=fs.readFileSync(path.join(root,'static/customer-statement-check.html'),'utf8');
 assert.ok(comparison.includes('ERPFinanceWorkspace.formatNumber(row.customer_amount)'));
 assert.ok(comparison.includes('ERPFinanceWorkspace.formatNumber(row.erp_quantity)'));
 for(const match of comparison.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))if(match[1].trim())assert.doesNotThrow(()=>new vm.Script(match[1]));
});

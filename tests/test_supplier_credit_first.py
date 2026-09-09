"""Execute the real page methods without making a payment or touching a DB."""
from pathlib import Path
import subprocess


def test_credit_first_payment_suggestions():
    html = Path('static/index.html').read_text(encoding='utf-8')
    def method(name, next_name):
        start = html.index('          '+name+'(')
        end = html.index('          '+next_name+'(', start)
        return html[start:end]
    methods = ''.join([
        method('hydrateSupplierSettlement', 'suggestSupplierCreditFirst'),
        method('suggestSupplierCreditFirst', 'supplierSettlementAcceptanceOptions'),
        method('supplierSettlementPaymentCapacity', 'supplierSettlementAcceptanceAppliedAmount'),
    ])
    js = "const assert=require('node:assert/strict');const today=()=> '2026-09-09';const vm={" + methods + "};" + r'''
function check(remaining, invoice, credits, expected, bank) {
 const input={remaining_payable_amount:remaining,available_payment_amount:invoice,
   available_credits:credits.map((a,i)=>({id:i+1,available_amount:a,version:7}))};
 const row=vm.hydrateSupplierSettlement(input);
 assert.deepEqual(row.available_credits.map(c=>c._applyAmount),expected);
 assert.equal(row._bankAmount,bank);
 assert.equal(row._acceptanceNoteId,null);
 assert.equal(input.available_credits[0]?._applyAmount,undefined);
 const snapshot=JSON.stringify(row);
 vm.suggestSupplierCreditFirst(row);
 assert.equal(JSON.stringify(row),snapshot);
 assert.ok(row.available_credits.every(c=>c.version===7));
}
check('12000','12000',['200'],['200.00'],'11800.00');
check('100','100',['120'],['100.00'],'0.00');
check('100','100',['20','30'],['20.00','30.00'],'50.00');
check('100','0',['20'],['0.00'],'0.00');
check('100','30',['20','30'],['20.00','10.00'],'0.00');
check('0.30','0.30',['0.10','0.20'],['0.10','0.20'],'0.00');
check('100','100',[],[],'100.00');
console.log('7 cases + replay/source preservation passed');
'''
    result = subprocess.run(['node','-e',js], capture_output=True,text=True,check=False)
    assert result.returncode == 0, result.stderr

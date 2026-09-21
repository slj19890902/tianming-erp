const fs=require('fs'),assert=require('assert');
const html=fs.readFileSync('static/index.html','utf8');
const body=html.split('invoiceBuyerRemark(task) {')[1].split('async copyInvoiceBuyerRemark() {')[0].replace(/},\s*$/, '');
const remark=new Function('task',body);
assert.equal(remark({buyer_snapshot:{}}),'');
assert.equal(remark({buyer_snapshot:{bank_account:'001234567890'}}),'购方银行账号：001234567890');
const text=remark({buyer_snapshot:{invoice_address:' 地址 ',invoice_phone:'0123',bank_name:'银行',bank_account:'00123'}});
assert.equal(text,'购方地址：地址；购方电话：0123；购方开户行：银行；购方银行账号：00123');
console.log('PASS frozen optional invoice remark fields, blanks and leading zeros');

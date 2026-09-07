from pathlib import Path
import json
import shutil
import subprocess


INDEX = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")


def body(start: str, end: str) -> str:
    return INDEX.split(start, 1)[1].split(end, 1)[0].rsplit("}", 1)[0]


def test_invoice_form_opens_without_writing_and_preserves_versions_and_retry() -> None:
    script = r"""
const assert=require('node:assert/strict');
const messages=[];
let posts=0;
globalThis.axios={post(){posts++; throw Error('unexpected write');}};
const ui={canFinance:true,financeStatementOperationState:{action:''},
 financeManualMutationAttempts:{register:null,settle:null},financeInvoiceForm:{row:null},
 showToast:message=>messages.push(message), financeManualMutationAttemptIsCurrent:()=>true};
ui.openRegisterInvoice = new Function('row', OPEN);
ui.registerInvoice = new (Object.getPrototypeOf(async function(){}).constructor)('row', SUBMIT);
const row={id:7,confirmation_status:'confirmed',total_receivable:100,invoiced_amount:20,ledger_version:3,version:2};
(async()=>{
 assert.equal(ui.openRegisterInvoice(row), true);
 assert.equal(posts,0); assert.equal(ui.financeInvoiceForm.amount,'80.00');
 row.version=99; assert.equal(ui.financeInvoiceForm.row.version,2);
 ui.modal=null; assert.equal(posts,0); // closing an untouched form has no command to replay
 ui.openRegisterInvoice(row);
 for (const amount of ['',null,0,81,'not-a-number']) {
   ui.financeInvoiceForm.invoice_number='TEST-INVOICE'; ui.financeInvoiceForm.amount=amount;
   assert.equal(await ui.registerInvoice(ui.financeInvoiceForm.row),false); assert.equal(posts,0);
 }
 ui.financeInvoiceForm.committed=true;
 assert.equal(ui.openRegisterInvoice(row),false); // old screen balance after a failed read
 row.ledger_version=4; assert.equal(ui.openRegisterInvoice(row),true);
 ui.financeManualMutationAttempts.register={statementId:7,payload:{invoice_number:'ORIGINAL',invoice_amount:'12.34'}};
 assert.equal(ui.openRegisterInvoice(row),true);
 assert.equal(ui.financeInvoiceForm.invoice_number,'ORIGINAL'); assert.equal(ui.financeInvoiceForm.amount,'12.34');
 assert.equal(ui.openRegisterInvoice({...row,id:8}),false);
 ui.financeManualMutationAttemptIsCurrent=()=>false;
 assert.equal(ui.openRegisterInvoice(row),false); assert.equal(posts,0);
})().catch(e=>{console.error(e);process.exit(1);});
""".replace("OPEN", json.dumps(body("openRegisterInvoice(row) {", "async registerInvoice(row) {"), ensure_ascii=False)).replace("SUBMIT", json.dumps(body("async registerInvoice(row) {", "async settle(row) {"), ensure_ascii=False))
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr

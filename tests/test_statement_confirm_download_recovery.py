"""Exercise the shipped statement workflow, including partially committed replies."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
BODY = INDEX.split("async confirmAndDownloadStatement(row) {", 1)[1].split(
    "invoiceBuyerRemark(task) {", 1
)[0].rsplit("}", 1)[0]


@pytest.mark.parametrize("scenario", ["missing", "conflict", "lost_reply", "download", "session", "refresh", "denied", "cancelled"])
def test_confirmation_download_recovery(tmp_path, scenario):
    script = r'''
const assert = require('node:assert/strict');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const scenario = process.argv[2], requests = [], messages = [];
let failTask = scenario === 'missing', downloads = 0, taskConfirms = 0;
let taskStatus = 'draft', failDownload = scenario === 'download';
const row = {id:58, customer_id:80, version:1, confirmation_status:'draft'};
const other = {id:59, version:7, confirmation_status:'draft'};
const vm = {
  authGeneration:1, user:{id:1}, canConfirmStatement:true, canGenerateInvoiceTask:true,
  statementDetail:{...row}, statements:[{...row}], financeCurrentRows:[{statements:[{...row}]}],
  financeStatementOperationState:{action:'',statementId:null},
  modal:{type:'statementDetail'},
  async loadFinance(){ if(scenario==='refresh') throw Error('list offline'); },
  errorMessage(e){ return e.response?.data?.detail?.message || e.message; },
  showToast(m){ messages.push(m); },
  async confirmInvoiceTask(task){ taskConfirms++; taskStatus='ready'; return {...task,status:'ready',version:2}; },
  async downloadInvoiceTaxTemplate(task){ downloads++; if(failDownload){failDownload=false;return false;} taskStatus='exported';return true; },
};
if(scenario==='denied') vm.canGenerateInvoiceTask=false;
if(scenario==='cancelled') row.confirmation_status='cancelled';
const conflict = () => Object.assign(Error('version changed'),{response:{status:409,data:{detail:{message:'对账单版本已变化'}}}});
globalThis.createIdempotencyKey = () => 'request-key';
globalThis.axios = {
  async get(url){
    requests.push({method:'GET',url});
    return {data:{...row,version:scenario==='conflict'?3:2,
      confirmation_status:scenario==='conflict'?'draft':'confirmed',total_receivable:1800}};
  },
  async post(url,payload){
    requests.push({method:'POST',url,payload});
    if(url.endsWith('/confirm')){
      if(scenario==='conflict') throw conflict();
      if(scenario==='lost_reply') throw Error('network timeout');
      if(scenario==='session') { vm.authGeneration++;vm.statementDetail=other; }
      return {data:{id:58,version:2,confirmation_status:'confirmed',ledger_version:1}};
    }
    assert.equal(payload.expected_version,2);
    if(failTask){ failTask=false;throw Object.assign(Error('missing'),{response:{status:409,data:{detail:{message:'客户缺少默认销方主体',missing_items:['默认销方主体']}}}}); }
    return {data:{id:20,statement_id:58,status:taskStatus,version:taskStatus==='draft'?1:2}};
  }
};
vm.confirmAndDownloadStatement = new AsyncFunction('row', BODY).bind(vm);
(async()=>{
  const first = vm.confirmAndDownloadStatement(row);
  assert.equal(await vm.confirmAndDownloadStatement(row),false,'double click must not duplicate writes');
  const result = await first;
  const confirms = () => requests.filter(r=>r.method==='POST' && r.url.endsWith('/confirm'));
  if(['denied','cancelled'].includes(scenario)){
    assert.equal(result,false);assert.equal(requests.length,0);
  } else if(['missing','download'].includes(scenario)){
    assert.equal(result,false);
    for(const target of [row,vm.statementDetail,vm.statements[0],vm.financeCurrentRows[0].statements[0]]){
      assert.equal(target.version,2,'committed confirmation version must reach the open detail and lists');
      assert.equal(target.confirmation_status,'confirmed');
    }
    vm.statementDetail=other;
    assert.equal(await vm.confirmAndDownloadStatement(row),true);
    assert.equal(confirms().length,1,'retry must resume after the committed confirmation');
    assert.deepEqual(vm.statementDetail,other,'a different detail must not be overwritten');
    assert.equal(taskConfirms,1,'ready task must download without re-confirmation');
    if(scenario==='missing') assert.ok(messages.some(m=>m.includes('默认销方主体')));
  } else if(scenario==='conflict'){
    assert.equal(result,false); assert.equal(confirms().length,1);
    assert.equal(requests.filter(r=>r.method==='POST').length,1,'never automatically confirm a newer version');
    assert.equal(vm.statementDetail.version,3);assert.equal(vm.statementDetail.total_receivable,1800);
  } else if(scenario==='lost_reply'){
    assert.equal(result,false); assert.equal(confirms().length,1);
    assert.equal(vm.statementDetail.confirmation_status,'confirmed');
    assert.equal(requests.filter(r=>r.method==='POST').length,1,'read recovery must wait for user retry');
    assert.equal(await vm.confirmAndDownloadStatement(row),true);assert.equal(confirms().length,1);
  } else if(scenario==='session'){
    assert.equal(result,false);assert.equal(requests.length,1,'do not continue writes under a changed login');
    assert.deepEqual(vm.statementDetail,other);
  } else if(scenario==='refresh'){
    assert.equal(result,true,'successful download must not be reported as failed when list reload fails');
    assert.equal(downloads,1);assert.equal(confirms().length,1);
  }
  assert.equal(vm.financeStatementOperationState.action,'');
})().catch(e=>{console.error(e);process.exit(1);});
'''.replace("BODY", json.dumps(BODY, ensure_ascii=False))
    path = tmp_path / "statement-download.cjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([shutil.which("node") or "node", str(path), scenario], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_confirmed_statement_has_resume_action_in_open_detail():
    footer = INDEX[INDEX.index('modal?.type===\'statementDetail\' && ![\'confirmed\',\'cancelled\']'):]
    footer = footer[:footer.index('invoiceBuyerRemark')]
    assert "继续下载开票文件" in footer

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("due,silent", [(True, True), (True, False), (False, False)])
def test_blocked_period_remains_visible_after_generation(tmp_path: Path, due: bool, silent: bool) -> None:
    start = "async generateDueSupplierSettlements(silent = false) {" if due else "async generateSupplierSettlements(silent = false) {"
    end = "async generateSupplierSettlements(silent = false) {" if due else "async mutateSupplierSettlement(row,action,url,payload,successMessage) {"
    body = INDEX.split(start, 1)[1].split(end, 1)[0].rsplit("}", 1)[0]
    script = f"""
const assert=require('node:assert/strict');
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.createIdempotencyKey=()=> 'isolated-completeness-key';
const gap={{source_key:'paperboard:2',receipt_number:'IR-GAP',code:'PAPERBOARD_FROZEN_PRICE_MISSING'}};
global.axios={{post:async()=>({{data:{{items:[],issues:[gap],completeness:{{status:'blocked',message:'1 家供应商账期存在缺口，已暂停生成'}}}}}})}};
const notices=[];
const vm={{canFinance:true,supplierSettlementMonth:'2026-08',supplierSettlementState:{{action:''}},
supplierSettlementIssues:[],supplierSettlements:[],hydrateSupplierSettlement:x=>x,
loadSupplierSettlements:async function(){{this.supplierSettlementIssues=[];this.supplierSettlementState.error='';}},
showToast:(message,warning)=>notices.push({{message,warning}})}};
const run=new AsyncFunction('silent',{json.dumps(body)}).bind(vm);
(async()=>{{
  await run({str(silent).lower()});
  assert.equal(vm.supplierSettlementState.error,'1 家供应商账期存在缺口，已暂停生成');
  assert.deepEqual(vm.supplierSettlementIssues,[gap]);
  assert.equal(vm.supplierSettlementState.action,'');
  assert.equal(notices.length,{0 if silent else 1});
  if(notices.length) assert.equal(notices[0].warning,true,'must not show a success-only toast');
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    node = shutil.which("node")
    assert node, "Node.js is required for financial completeness UI checks"
    target = tmp_path / "monthly-completeness.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_completeness_error_preserves_actionable_details() -> None:
    from app.api.supplier_settlements import _translate
    from app.services.supplier_monthly_settlement import SupplierSettlementError

    details = {"completeness": {"status": "blocked", "issue_count": 1, "period_end": date(2026, 8, 20)}, "issues": [{"receipt_number": "IR-GAP"}]}
    error = SupplierSettlementError("SUPPLIER_SETTLEMENT_INCOMPLETE", "账期存在缺口", status_code=409, details=details)
    response = _translate(error)
    assert response.status_code == 409
    assert response.detail["code"] == error.code
    assert response.detail["message"] == error.message
    assert response.detail["issues"] == details["issues"]
    assert response.detail["completeness"]["period_end"] == "2026-08-20"
    json.dumps(response.detail)

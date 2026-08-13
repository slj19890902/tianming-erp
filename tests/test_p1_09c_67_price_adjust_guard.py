from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX, f"missing Vue method: {signature}"
    assert next_signature in INDEX, f"missing Vue method boundary: {next_signature}"
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the price-adjust regression"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_price_adjust_ui_distinguishes_preview_and_apply_state() -> None:
    assert 'priceAdjustAction: ""' in INDEX
    assert "priceAdjustAction === 'preview' ? '预览中…'" in INDEX
    assert "priceAdjustAction === 'apply' ? '应用中…'" in INDEX
    assert ':disabled="priceAdjustAction === \'apply\'"' in INDEX
    assert '@click.self="closePriceAdjust()"' in INDEX
    assert ':disabled="priceAdjustAction === \'apply\'" @click="closePriceAdjust()"' in INDEX
    watcher = INDEX.split("priceAdjustForm: {\n            deep: true", 1)[1].split("incomingPending:", 1)[0]
    assert "this.priceAdjustLoading" in watcher
    assert "this.invalidatePriceAdjustPreview()" in watcher


def test_price_adjust_modal_cannot_reopen_or_close_during_apply() -> None:
    opener = _method_body("openPriceAdjust() {", "async previewPriceAdjust() {")
    closer = _method_body("closePriceAdjust() {", "openPriceAdjust() {")
    assert 'if (this.priceAdjustAction === "apply") return false;' in opener
    assert 'if (this.priceAdjustAction === "apply") return false;' in closer
    assert "this.invalidatePriceAdjustPreview();" in closer
    assert "this.showPriceAdjustModal = false;" in closer


def test_latest_price_adjust_preview_cannot_cross_form_conditions(tmp_path: Path) -> None:
    preview_body = _method_body("async previewPriceAdjust() {", "async applyPriceAdjust() {")
    invalidate_body = _method_body("invalidatePriceAdjustPreview() {", "priceAdjustRequestBody(")
    assert 'const requestKey = "materials:price-adjust-preview";' in preview_body
    assert "const requestFormKey = this.priceAdjustFormKey();" in preview_body
    assert "signal:controller.signal" in preview_body
    assert "this.priceAdjustFormKey() !== requestFormKey" in preview_body
    assert "this.priceAdjustPreviewKey = requestFormKey" in preview_body

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();
const pending=[];
global.axios={{post:(url,body,options)=>new Promise((resolve,reject)=>pending.push({{url,body,options,resolve,reject}}))}};
const vm={{
  priceAdjustForm:{{supplier_name:"供应商A",adjust_percent:"5",effective_date:"2026-08-05"}},
  priceAdjustPreview:null,priceAdjustPreviewKey:"",priceAdjustError:"",priceAdjustLoading:false,priceAdjustAction:"",showPriceAdjustModal:true,
  priceAdjustFormKey(){{const f=this.priceAdjustForm;return JSON.stringify({{supplier_name:String(f.supplier_name||"").trim(),adjust_percent:String(f.adjust_percent||"").trim(),effective_date:String(f.effective_date||"")}});}},
  priceAdjustRequestBody(){{return {{supplier_name:this.priceAdjustForm.supplier_name,adjust_percent:this.priceAdjustForm.adjust_percent,effective_date:this.priceAdjustForm.effective_date}};}},
  beginLatestRequest(key){{global.latestRequestControllers.get(key)?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="CanceledError";}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.invalidatePriceAdjustPreview=new Function({json.dumps(invalidate_body, ensure_ascii=False)}).bind(vm);
vm.previewPriceAdjust=new AsyncFunction({json.dumps(preview_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.previewPriceAdjust();
  vm.priceAdjustForm={{supplier_name:"供应商B",adjust_percent:"-3",effective_date:"2026-08-06"}};
  vm.invalidatePriceAdjustPreview();
  const second=vm.previewPriceAdjust();
  pending[1].resolve({{data:{{supplier_name:"供应商B",adjust_percent:-3,expected_versions:{{2:1}}}}}});
  expect(await second===true,"latest preview did not complete");
  pending[0].resolve({{data:{{supplier_name:"供应商A",adjust_percent:5,expected_versions:{{1:1}}}}}});
  expect(await first===false,"stale preview was accepted");
  expect(vm.priceAdjustPreview?.supplier_name==="供应商B","stale preview overwrote latest result");
  expect(vm.priceAdjustPreviewKey===vm.priceAdjustFormKey(),"latest preview key does not match its form snapshot");
  expect(vm.priceAdjustLoading===false&&vm.priceAdjustAction==="","preview state was not released");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "price-adjust-preview-race.js")


def test_apply_price_adjust_is_single_flight_freezes_contract_and_reports_refresh_failure(tmp_path: Path) -> None:
    body = _method_body("async applyPriceAdjust() {", "reloadPriceHistoryFilters() {")
    assert "if (this.priceAdjustLoading || this.priceAdjustAction) return false;" in body
    assert "const formSnapshot =" in body
    assert "const body = JSON.parse(JSON.stringify(this.priceAdjustRequestBody(true)))" in body
    assert "调价已完成，但材质列表刷新失败" in body

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,release;const notices=[];
global.confirm=()=>true;global.alert=message=>notices.push([message,false]);
global.axios={{post:(url,body)=>new Promise(resolve=>{{writes+=1;global.saved={{url,body}};release=resolve;}})}};
const vm={{
  priceAdjustPreviewValid:true,priceAdjustLoading:false,priceAdjustAction:"",priceAdjustError:"",showPriceAdjustModal:true,
  priceAdjustForm:{{supplier_name:"供应商A",adjust_percent:"5",effective_date:"2026-08-05"}},
  priceAdjustPreview:{{expected_versions:{{1:3}},preview_token:"token-a"}},priceAdjustPreviewKey:"key-a",
  priceAdjustFormKey(){{return "key-a";}},
  priceAdjustRequestBody(){{return {{supplier_name:this.priceAdjustForm.supplier_name,adjust_percent:this.priceAdjustForm.adjust_percent,effective_date:this.priceAdjustForm.effective_date,expected_versions:this.priceAdjustPreview.expected_versions,preview_token:this.priceAdjustPreview.preview_token}};}},
  allMaterials:[{{id:1}}],materialSuppliers:["供应商A"],loadMaterials:async()=>false,
  showToast(message,error=false){{notices.push([message,error]);}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.applyPriceAdjust=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.applyPriceAdjust();
  const duplicate=await vm.applyPriceAdjust();
  vm.priceAdjustForm={{supplier_name:"供应商B",adjust_percent:"99",effective_date:"2026-09-01"}};
  vm.priceAdjustPreview={{expected_versions:{{9:9}},preview_token:"token-b"}};
  expect(duplicate===false&&writes===1,"double apply issued another mutation");
  release({{data:{{affected_count:3,batch_id:8}}}});
  expect(await first===true,"committed adjustment plus refresh failure was reported as write failure");
  expect(global.saved.body.supplier_name==="供应商A"&&global.saved.body.adjust_percent==="5","apply did not freeze form values");
  expect(global.saved.body.preview_token==="token-a"&&global.saved.body.expected_versions[1]===3,"apply did not freeze preview contract");
  expect(vm.showPriceAdjustModal===false,"committed adjustment modal remained open");
  expect(notices.some(([message,error])=>message.includes("调价已完成")&&message.includes("列表刷新失败")&&message.includes("不要重复应用")&&error===true),"partial success guidance missing");
  expect(vm.priceAdjustLoading===false&&vm.priceAdjustAction==="","apply state was not released");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "price-adjust-apply-guard.js")


def test_price_adjust_keeps_preview_contract_and_backup_confirmation() -> None:
    apply_body = _method_body("async applyPriceAdjust() {", "reloadPriceHistoryFilters() {")
    assert "expected_versions" in INDEX
    assert "preview_token" in INDEX
    assert "confirmation_token" in INDEX
    assert "confirmation_tokens" in INDEX
    assert "将先备份数据库并写入价格历史" in apply_body
    assert "priceAdjustForm.change_reason" not in INDEX

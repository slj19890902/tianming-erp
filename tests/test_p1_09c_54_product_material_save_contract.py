from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the master save regression"
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


def test_product_and_material_save_button_exposes_shared_single_flight_state() -> None:
    footer = INDEX[INDEX.index('<div class="modal-foot">') : INDEX.index('</div>\n        </div>\n      </div>', INDEX.index('<div class="modal-foot">'))]
    assert "masterSavePending" in INDEX
    assert ":disabled=\"loading || masterSavePending ||" in footer
    assert "['product','material'].includes(modal?.type) && masterSavePending ? '保存中…'" in footer


def test_product_and_material_save_guard_starts_before_master_preflight() -> None:
    save = _method_body("async saveModal() {", "async dispatchDelivery(row) {")
    guard = save.index("if (masterSaveEntity && this.masterSavePending)")
    lock = save.index("this.masterSavePending = true")
    preflight = save.index("await this.prepareProductOneClickSave()")
    assert guard < lock < preflight
    assert "正在保存，请勿重复点击" in save
    assert "if (masterSaveEntity) this.masterSavePending = false" in save


def test_product_save_runtime_blocks_duplicate_before_preflight(tmp_path: Path) -> None:
    save_body = _method_body("async saveModal() {", "async dispatchDelivery(row) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let preflightCalls=0,releasePreflight;
const notices=[];
const vm={{
  modal:{{type:"product"}},masterSavePending:false,masterPendingSaveOptions:null,
  masterChangeConfirm:{{entity:null}},loading:false,
  masterCurrentForm(){{return {{id:7}};}},masterLocalChanges(){{return [{{field:"product_name"}}];}},
  prepareProductOneClickSave(){{preflightCalls+=1;return new Promise(resolve=>releasePreflight=resolve);}},
  showToast(message,error=false){{notices.push([message,error]);}}
}};
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveModal();
  const duplicate=await vm.saveModal();
  expect(duplicate===false,"duplicate save was not rejected");
  expect(preflightCalls===1,"duplicate save issued a second preflight");
  expect(notices.some(([message])=>message.includes("请勿重复点击")),"duplicate feedback missing");
  releasePreflight({{completed:true,options:null}});
  expect(await first===true,"first save did not finish normally");
  expect(vm.masterSavePending===false,"single-flight state was not released");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-save-single-flight.js")


def test_successful_product_write_with_refresh_failure_is_not_reported_as_save_failure(
    tmp_path: Path,
) -> None:
    refresh_body = _method_body(
        "handleMasterSaveRefreshFailure(entity, error) {", "async saveModal() {"
    )
    save_body = _method_body("async saveModal() {", "async dispatchDelivery(row) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
let writes=0,closed=0;const notices=[];
global.axios={{post:async()=>{{writes+=1;return {{data:{{id:9,version:1}}}};}}}};
const vm={{
  modal:{{type:"product"}},masterSavePending:false,masterPendingSaveOptions:null,
  masterChangeConfirm:{{entity:null}},loading:false,productForm:{{id:null}},drawingFile:null,
  productEditReturnContext:null,productFormSnapshot:"dirty",
  masterCurrentForm(){{return this.productForm;}},_productFormDirty(){{return true;}},_productBomDirty(){{return false;}},
  buildProductWritePayload(){{return {{}};}},loadProducts:async()=>{{throw new Error("列表超时");}},
  closeModal(){{closed+=1;this.modal=null;}},showToast(message,error=false){{notices.push([message,error]);}},
  errorMessage(error){{return error?.message||String(error);}},handleMaster409(){{return false;}}
}};
vm.handleMasterSaveRefreshFailure=new FunctionCtor("entity","error",{json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.saveModal()===true,"refresh failure changed successful write to failure");
  expect(writes===1,"product write count changed");expect(closed===1,"saved editor remained open");
  expect(notices.some(([message,error])=>message.includes("业务已成功")&&message.includes("请勿重复提交")&&message.includes("手动刷新核对")&&error===true),"safe refresh failure notice missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-refresh-failure.js")


def test_successful_material_write_with_refresh_failure_uses_same_contract(
    tmp_path: Path,
) -> None:
    refresh_body = _method_body(
        "handleMasterSaveRefreshFailure(entity, error) {", "async saveModal() {"
    )
    save_body = _method_body("async saveModal() {", "async dispatchDelivery(row) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,closed=0;const notices=[];
global.axios={{post:async()=>{{writes+=1;return {{data:{{id:3}}}};}}}};
const vm={{
  modal:{{type:"material"}},masterSavePending:false,masterPendingSaveOptions:null,
  masterChangeConfirm:{{entity:null}},loading:false,materialForm:{{id:null}},
  masterCurrentForm(){{return this.materialForm;}},buildMaterialWritePayload(){{return {{code:"A+A"}};}},
  attachMasterUpdateMetadata(entity,payload){{return payload;}},loadMaterials:async()=>{{throw new Error("刷新断开");}},
  closeModal(){{closed+=1;this.modal=null;}},showToast(message,error=false){{notices.push([message,error]);}},
  errorMessage(error){{return error?.message||String(error);}},handleMaster409(){{return false;}}
}};
vm.handleMasterSaveRefreshFailure=new Function("entity","error",{json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.saveModal()===true,"material refresh failure changed successful write to failure");
  expect(writes===1&&closed===1,"material write/close contract failed");
  expect(notices.some(([message])=>message.includes("材质已保存")&&message.includes("手动刷新核对")),"material refresh failure notice missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "material-refresh-failure.js")

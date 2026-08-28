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
    assert node is not None, "Node.js is required for the list contract regression"
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


def test_product_and_material_loaders_expose_latest_request_contract() -> None:
    products = _method_body("async loadProducts() {", "async loadMoldTools() {")
    materials = _method_body("async loadMaterials() {", "async openMaterialCandidateMaintenance() {")
    for block, key in ((products, "products:list"), (materials, "materials:list")):
        assert f'const requestKey = "{key}"' in block
        assert "latestRequestControllers.get(requestKey) !== controller) return false" in block
        assert "return true;" in block
        assert "return false;" in block
        assert "this.finishLatestRequest(requestKey, controller)" in block


def test_product_list_runtime_reports_result_and_keeps_latest_response(tmp_path: Path) -> None:
    body = _method_body("async loadProducts() {", "async loadMoldTools() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  activePage:"products",productTab:"products",selectedProductCustomer:{{id:5}},
  filters:{{productKeyword:"A",productCode:"",productName:"",productSpec:"",productMaterial:"",showInactiveProducts:false,productCustomer:null}},
  pages:{{products:1}},pageSize:25,products:[],productsTotal:0,productsLoading:false,productsError:"",
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.loadProducts=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadProducts();vm.filters.productKeyword="B";const second=vm.loadProducts();
  pending[1].resolve({{data:{{items:[{{id:2,product_code:"B"}}],total:1}}}});
  expect(await second===true,"latest product list did not report success");
  pending[0].resolve({{data:{{items:[{{id:1,product_code:"A"}}],total:1}}}});
  expect(await first===false,"old product list was treated as current");
  expect(vm.products[0].id===2&&vm.productsTotal===1,"old product list overwrote latest result");
  const failed=vm.loadProducts();pending[2].reject(new Error("列表断开"));
  expect(await failed===false,"product load failure did not report false");
  expect(vm.productsError.includes("列表断开"),"product load error feedback missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-list-latest.js")


def test_material_list_runtime_discards_ignored_abort_response(tmp_path: Path) -> None:
    body = _method_body("async loadMaterials() {", "async openMaterialCandidateMaintenance() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
const vm={{
  materialSort:"common",materialLayerFilter:"",materialSupplierFilter:"鸣朋",materials:[],
  allMaterials:[{{id:99}}],materialSuppliers:[],activeSupplierNames:["鸣朋"],
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},
  fetchAllMaterials(params,signal){{return new Promise((resolve,reject)=>pending.push({{params,signal,resolve,reject}}));}}
}};
vm.loadMaterials=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadMaterials();vm.materialSupplierFilter="嘉林亿";const second=vm.loadMaterials();
  pending[1].resolve([{{id:2,supplier_name:"嘉林亿"}}]);
  expect(await second===true,"latest material list did not report success");
  pending[0].resolve([{{id:1,supplier_name:"鸣朋"}}]);
  expect(await first===false,"old material list was treated as current");
  expect(vm.materials[0].id===2,"old material list overwrote latest result");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "material-list-latest.js")


def test_product_save_treats_false_refresh_as_success_warning(tmp_path: Path) -> None:
    refresh_body = _method_body("handleMasterSaveRefreshFailure(entity, error) {", "moldRepairWarningText(warnings) {")
    save_body = _method_body("async saveModal() {", "async dispatchDelivery(row, options = {}) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let closed=0;const notices=[];
global.axios={{post:async()=>({{data:{{id:9,version:1}}}})}};
const vm={{
  modal:{{type:"product"}},masterSavePending:false,masterPendingSaveOptions:null,masterChangeConfirm:{{entity:null}},loading:false,
  productForm:{{id:null}},drawingFile:null,productEditReturnContext:null,productFormSnapshot:"dirty",
  masterCurrentForm(){{return this.productForm;}},_productFormDirty(){{return true;}},_productBomDirty(){{return false;}},
  buildProductWritePayload(){{return {{}};}},loadProducts:async()=>false,
  closeModal(){{closed+=1;this.modal=null;}},showToast(message,error=false){{notices.push([message,error]);}},
  errorMessage(error){{return error?.message||String(error);}},handleMaster409(){{return false;}}
}};
vm.handleMasterSaveRefreshFailure=new Function("entity","error",{json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.saveModal()===true,"false refresh changed successful product write");
  expect(closed===1,"saved product editor remained open");
  expect(notices.some(([message,error])=>message.includes("常用箱已保存")&&message.includes("业务已成功")&&message.includes("请勿重复提交")&&error===true),"product false-refresh warning missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-save-false-refresh.js")


def test_material_save_treats_false_refresh_as_success_warning(tmp_path: Path) -> None:
    refresh_body = _method_body("handleMasterSaveRefreshFailure(entity, error) {", "moldRepairWarningText(warnings) {")
    save_body = _method_body("async saveModal() {", "async dispatchDelivery(row, options = {}) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let closed=0;const notices=[];
global.axios={{post:async()=>({{data:{{id:3}}}})}};
const vm={{
  modal:{{type:"material"}},masterSavePending:false,masterPendingSaveOptions:null,masterChangeConfirm:{{entity:null}},loading:false,
  materialForm:{{id:null}},masterCurrentForm(){{return this.materialForm;}},
  buildMaterialWritePayload(){{return {{code:"A+A"}};}},attachMasterUpdateMetadata(entity,payload){{return payload;}},loadMaterials:async()=>false,
  closeModal(){{closed+=1;this.modal=null;}},showToast(message,error=false){{notices.push([message,error]);}},
  errorMessage(error){{return error?.message||String(error);}},handleMaster409(){{return false;}}
}};
vm.handleMasterSaveRefreshFailure=new Function("entity","error",{json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
vm.saveModal=new AsyncFunction({json.dumps(save_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.saveModal()===true,"false refresh changed successful material write");
  expect(closed===1,"saved material editor remained open");
  expect(notices.some(([message,error])=>message.includes("材质已保存")&&message.includes("业务已成功")&&message.includes("请勿重复提交")&&error===true),"material false-refresh warning missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "material-save-false-refresh.js")

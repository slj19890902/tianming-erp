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
    assert node is not None, "Node.js is required for the product editor regression"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_product_editor_loaders_declare_latest_request_contract() -> None:
    molds = _method_body("async loadMoldTools() {", "async ensureProductEditorOptions(")
    options = _method_body("async ensureProductEditorOptions({force=false} = {}) {", "moldToolSelectOptions() {")
    bom = _method_body("async loadProductBom(productId) {", "async searchBomProducts(")
    editor = _method_body("async openProduct(row=null) {", "async loadProductMaterialContext(")

    assert 'const requestKey = "product:molds"' in molds
    assert "latestRequestControllers.get(requestKey) !== controller) return false" in molds
    assert 'const requestKey = "product:editor-options"' in options
    assert "results.some(result => result !== true)" in options
    assert 'const requestKey = "product:bom"' in bom
    assert "Number(this.productForm.id || 0) !== requestedId" in bom
    assert 'const requestKey = "product:editor"' in editor
    assert "signal:controller.signal" in editor
    assert "latestRequestControllers.get(requestKey) !== controller) return false" in editor


def test_open_product_runtime_discards_old_detail_response(tmp_path: Path) -> None:
    body = _method_body("async openProduct(row=null) {", "async loadProductMaterialContext(")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];const notices=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  productForm:{{id:null}},selectedProductCustomer:null,activeCustomerOptions:[],productMaterialContext:{{}},drawingFile:null,
  canEditProducts:true,bomEditor:{{}},productFormSnapshot:null,modal:null,
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}},
  loadProductBoxTypeRules:async()=>[],hydrateProductForm(row){{return {{...row}};}},resetBomEditor(){{}},
  _productFormSaveFields(){{return {{id:this.productForm.id}};}},beginMasterEdit(){{}},
  $nextTick(callback){{if(callback)callback();return Promise.resolve();}},ensureProductEditorOptions:async()=>true,
  loadProductBom:async()=>true,hasPermission(){{return false;}},autoApplyProductRecommendations(){{}},
  showToast(message,error=false){{notices.push([message,error]);}}
}};
vm.openProduct=new AsyncFunction("row",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.openProduct({{id:1}});const second=vm.openProduct({{id:2}});
  pending[1].resolve({{data:{{id:2,product_code:"B"}}}});expect(await second===true,"latest product editor did not report success");
  expect(vm.productForm.id===2,"latest product was not opened");
  pending[0].resolve({{data:{{id:1,product_code:"A"}}}});expect(await first===false,"old product detail was treated as current");
  expect(vm.productForm.id===2&&vm.productForm.product_code==="B","old product detail replaced latest editor");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-editor-detail-race.js")


def test_product_bom_runtime_discards_other_product_response(tmp_path: Path) -> None:
    body = _method_body("async loadProductBom(productId) {", "async searchBomProducts(")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  productForm:{{id:1}},bomEditor:{{loading:false,error:"",notice:""}},productBomSnapshot:null,applied:null,
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}},
  applyBomResponse(data){{this.applied=data.tag;}},_productBomSaveFields(){{return {{}};}}
}};
vm.loadProductBom=new AsyncFunction("productId",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadProductBom(1);vm.productForm.id=2;const second=vm.loadProductBom(2);
  pending[1].resolve({{data:{{tag:"B"}}}});expect(await second===true&&vm.applied==="B","latest BOM did not load");
  pending[0].resolve({{data:{{tag:"A"}}}});expect(await first===false,"old BOM was treated as current");
  expect(vm.applied==="B","old BOM replaced current product BOM");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-bom-race.js")


def test_mold_loader_runtime_keeps_latest_response(tmp_path: Path) -> None:
    body = _method_body("async loadMoldTools() {", "async ensureProductEditorOptions(")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  isWorkshop:false,moldTools:[],
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}}
}};
vm.loadMoldTools=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadMoldTools();const second=vm.loadMoldTools();
  pending[1].resolve({{data:{{items:[{{id:2}}]}}}});expect(await second===true,"latest molds did not report success");
  pending[0].resolve({{data:{{items:[{{id:1}}]}}}});expect(await first===false,"old molds were treated as current");
  expect(vm.moldTools[0].id===2,"old molds replaced latest list");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-mold-race.js")


def test_product_editor_options_reports_child_false_as_retryable_error(tmp_path: Path) -> None:
    body = _method_body("async ensureProductEditorOptions({force=false} = {}) {", "moldToolSelectOptions() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();
const vm={{
  isWorkshop:false,allMaterials:[],moldTools:[],productEditorOptionsLoading:false,productEditorOptionsError:"",
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},loadMaterials:async()=>false,loadMoldTools:async()=>true
}};
vm.ensureProductEditorOptions=new AsyncFunction("{{force=false}}={{}}",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.ensureProductEditorOptions()===false,"failed child load was treated as ready");
  expect(vm.productEditorOptionsError.includes("读取失败"),"retryable editor option error missing");
  expect(vm.productEditorOptionsLoading===false,"editor option loading state was not released");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "product-editor-options-false.js")

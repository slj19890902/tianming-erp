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
    assert node is not None, "Node.js is required for the BOM/context race regression"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bom_search_and_material_context_declare_latest_request_contract() -> None:
    search = _method_body("async searchBomProducts(keyword=\"\") {", "addBomComponent() {")
    context = _method_body("async loadProductMaterialContext(productId) {", "productMaterialCandidateKey(candidate) {")

    assert 'const requestKey = "product:bom-search"' in search
    assert "requestedProductId" in search and "requestedCustomerId" in search
    assert "latestRequestControllers.get(requestKey) !== controller" in search
    assert "signal:controller.signal" in search
    assert 'const requestKey = "product:material-context"' in context
    assert "latestRequestControllers.get(requestKey) !== controller" in context
    assert "signal:controller.signal" in context
    assert ':disabled="productMaterialContext.loading"' in INDEX
    assert "productMaterialContext.loading ? '读取中…' : '刷新'" in INDEX


def test_bom_search_runtime_keeps_latest_keyword_and_parent(tmp_path: Path) -> None:
    body = _method_body("async searchBomProducts(keyword=\"\") {", "addBomComponent() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  productForm:{{id:10,customer_id:5}},bomEditor:{{componentOptions:[],components:[],error:""}},
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}},
  bomComponentOption(row){{return {{...row,_label:row.product_code}};}},mergeBomComponentOptions(){{}}
}};
vm.searchBomProducts=new AsyncFunction("keyword",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.searchBomProducts("A");vm.productForm={{id:11,customer_id:5}};const second=vm.searchBomProducts("B");
  pending[1].resolve({{data:{{items:[{{id:2,product_code:"B"}}]}}}});expect(await second===true,"latest BOM search did not report success");
  expect(vm.bomEditor.componentOptions[0].id===2,"latest BOM search did not write options");
  pending[0].resolve({{data:{{items:[{{id:1,product_code:"A"}}]}}}});expect(await first===false,"old BOM search was treated as current");
  expect(vm.bomEditor.componentOptions[0].id===2,"old BOM search replaced latest options");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "bom-search-race.js")


def test_material_context_runtime_ignores_old_failure_after_new_success(tmp_path: Path) -> None:
    body = _method_body("async loadProductMaterialContext(productId) {", "productMaterialCandidateKey(candidate) {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const vm={{
  productForm:{{id:10}},productMaterialContext:{{product_id:10,candidates:[],requisition_history:[],manual_selection_history:[],loading:false,error:""}},
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}},
  productMaterialCandidateKey(candidate){{return String(candidate?.material_id||"");}},
  productMaterialCandidateDetailKeys:[],productMaterialCompareKeys:[]
}};
vm.loadProductMaterialContext=new AsyncFunction("productId",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadProductMaterialContext(10);const second=vm.loadProductMaterialContext(10);
  pending[1].resolve({{data:{{candidates:[{{material_id:2}}],requisition_history:[{{id:8}}],manual_selection_history:[]}}}});
  expect(await second===true,"latest material context did not report success");
  pending[0].reject(new Error("旧请求断开"));expect(await first===false,"old context failure was treated as current");
  expect(vm.productMaterialContext.candidates[0].material_id===2&&vm.productMaterialContext.error==="","old failure cleared latest context");
  const failed=vm.loadProductMaterialContext(10);pending[2].reject(new Error("当前请求断开"));
  expect(await failed===false,"current context failure did not report false");
  expect(vm.productMaterialContext.error.includes("当前请求断开"),"current context failure feedback missing");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path, "material-context-race.js")

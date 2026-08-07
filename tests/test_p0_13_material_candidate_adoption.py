from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P0-13 regression"
    target = tmp_path / "p0-13-material-adoption.js"
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


def test_candidate_adoption_declares_authoritative_atomic_contract() -> None:
    body = _method_body(
        "async adoptProductMaterialCandidate(candidate) {",
        "normalizeProductDimensions(form) {",
    )
    assert 'const requestKey = "product:material-adoption"' in body
    assert "product_version" in body
    assert "/api/master/materials/${materialId}" in body
    assert "/api/master/suppliers/candidates" in body
    assert "materialSupplier !== candidateSupplier" in body
    assert "currentLayer !== candidateLayer" in body
    assert "currentFlute !== candidateFlute" in body
    assert "Object.assign(this.productForm" in body
    assert "saveProduct" not in body
    assert 'productMaterialAdoption: { product_id:null, candidate_key:"", loading:false }' in INDEX
    assert ':disabled="candidate.selectable === false || productMaterialAdoption.loading"' in INDEX


def test_candidate_adoption_runtime_is_latest_exact_and_fail_closed(tmp_path: Path) -> None:
    body = _method_body(
        "async adoptProductMaterialCandidate(candidate) {",
        "normalizeProductDimensions(form) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();
const pending=[];
global.axios={{get(url,options){{return new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}));}}}};
const supplier={{standard_name:"供应商乙",is_active:true,aliases:[]}};
const vm={{
  productForm:{{id:7,version:3,material_id:1,_material_supplier:"供应商甲",layer_count:3,flute_type:"B"}},
  productMaterialAdoption:{{product_id:null,candidate_key:"",loading:false}},suppliers:[],allMaterials:[],materials:[],toasts:[],
  productMaterialCandidateKey(row){{return String(row?.candidate_key||row?.material_id||"");}},
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
  finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}},
  showToast(message,error=false){{this.toasts.push({{message,error}});}}
}};
    vm.adoptProductMaterialCandidate=new AsyncFunction("candidate",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
const answer=(start,key,id,supplierName="供应商乙",layer=3,flute="B")=>{{
  pending[start].resolve({{data:{{product_version:3,candidates:[{{candidate_key:key,material_id:id,material_code:"A",supplier_name:supplierName,layer_count:layer,flute_type:flute,selectable:true}}]}}}});
  pending[start+1].resolve({{data:{{id,code:"A",supplier_name:supplierName,layer_count:layer,is_active:true}}}});
  pending[start+2].resolve({{data:[supplier]}});
}};
(async()=>{{
  const first=vm.adoptProductMaterialCandidate({{candidate_key:"A",material_id:2,selectable:true}});
  const second=vm.adoptProductMaterialCandidate({{candidate_key:"B",material_id:3,selectable:true}});
  answer(3,"B",3);expect(await second===true,"latest candidate did not apply");
  answer(0,"A",2);expect(await first===false,"old candidate was treated as current");
  expect(vm.productForm.material_id===3&&vm.productForm._material_supplier==="供应商乙","old candidate replaced exact latest material/supplier");
  expect(vm.productForm.flute_type==="B"&&vm.productForm.layer_count===3,"confirmed layer/flute changed");

  const before=JSON.stringify(vm.productForm);
  const conflict=vm.adoptProductMaterialCandidate({{candidate_key:"C",material_id:4,selectable:true}});
  answer(6,"C",4,"供应商乙",5,"AB");expect(await conflict===false,"conflicting candidate applied");
  expect(JSON.stringify(vm.productForm)===before,"conflict partially changed form");

  const stale=vm.adoptProductMaterialCandidate({{candidate_key:"D",material_id:5,selectable:true}});
  pending[9].resolve({{data:{{product_version:4,candidates:[{{candidate_key:"D",material_id:5,material_code:"A",supplier_name:"供应商乙",layer_count:3,flute_type:"B",selectable:true}}]}}}});
  pending[10].resolve({{data:{{id:5,code:"A",supplier_name:"供应商乙",layer_count:3,is_active:true}}}});
  pending[11].resolve({{data:[supplier]}});
  expect(await stale===false,"stale product version applied");
  expect(JSON.stringify(vm.productForm)===before,"stale response changed form");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(script, tmp_path)


def test_material_context_exposes_product_version_for_stale_guard() -> None:
    source = (ROOT / "app" / "api" / "requisition.py").read_text(encoding="utf-8")
    endpoint = source.split('def product_material_context(', 1)[1].split('@router.get("/pending/{item_id}/material-history")', 1)[0]
    assert '"product_version": product.version' in endpoint

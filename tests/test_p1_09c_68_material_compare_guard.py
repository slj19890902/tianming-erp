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
    assert node is not None, "Node.js is required for the material-compare regression"
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


def test_compare_modal_exposes_truthful_loading_and_invalidates_changed_filters() -> None:
    modal = INDEX.split("<!-- 比价弹窗 -->", 1)[1].split("<!-- 楞型加价规则维护弹窗 -->", 1)[0]
    assert '@click.self="closeCompare()"' in modal
    assert '@click="closeCompare()"' in modal
    assert 'v-model.trim="compareKeyword" @input="invalidateCompareResults()"' in modal
    assert 'v-model.trim="compareWeight" @input="invalidateCompareResults()"' in modal
    assert ':disabled="compareLoading" @click="loadCompare"' in modal
    assert "compareLoading ? '查询中…' : '查询比价'" in modal


def test_compare_latest_request_wins_and_old_finally_cannot_clear_loading(tmp_path: Path) -> None:
    load_body = _method_body("async loadCompare() {", "compareFluteOptions() {")
    invalidate_body = _method_body("invalidateCompareResults() {", "closeCompare() {")
    assert 'const requestKey = "materials:compare";' in load_body
    assert "const requestSnapshotKey = this.compareRequestKey();" in load_body
    assert "signal:controller.signal" in load_body
    assert "this.compareRequestKey() !== requestSnapshotKey" in load_body
    assert "latestRequestControllers.get(requestKey) === controller" in load_body

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();
const pending=[];
global.axios={{post:(url,body,options)=>new Promise((resolve,reject)=>pending.push({{url,body,options,resolve,reject}}))}};
const vm={{
  allMaterials:[{{id:1,code:"AAA",basis_weight_description:"A重"}},{{id:2,code:"BBB",basis_weight_description:"B重"}}],materials:[],
  materialSupplierFilter:"",materialLayerFilter:3,compareKeyword:"AAA",compareWeight:"",compareGroups:[],compareLoading:false,compareError:"",showCompareModal:true,
  compareRequestKey(){{return JSON.stringify({{supplier_name:this.materialSupplierFilter||null,layer_count:this.materialLayerFilter||null,keyword:String(this.compareKeyword||"").trim().toUpperCase(),weight:String(this.compareWeight||"").trim()}});}},
  beginLatestRequest(key){{global.latestRequestControllers.get(key)?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="CanceledError";}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.invalidateCompareResults=new Function({json.dumps(invalidate_body, ensure_ascii=False)}).bind(vm);
vm.loadCompare=new AsyncFunction({json.dumps(load_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadCompare();
  vm.compareKeyword="BBB";vm.invalidateCompareResults();
  const second=vm.loadCompare();
  pending[0].resolve({{data:{{groups:[{{name:"A旧结果"}}]}}}});
  expect(await first===false,"stale compare request was accepted");
  expect(vm.compareLoading===true,"stale request finally cleared the latest loading state");
  pending[1].resolve({{data:{{groups:[{{name:"B新结果"}}]}}}});
  expect(await second===true,"latest compare request did not complete");
  expect(vm.compareGroups.length===1&&vm.compareGroups[0].name==="B新结果","latest compare result was not retained");
  expect(vm.compareLoading===false&&vm.compareError==="","latest request did not release cleanly");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "material-compare-latest.js")


def test_close_compare_cancels_pending_request_and_blocks_late_result(tmp_path: Path) -> None:
    load_body = _method_body("async loadCompare() {", "compareFluteOptions() {")
    close_body = _method_body("closeCompare() {", "openCompare() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();let release;
global.axios={{post:()=>new Promise(resolve=>{{release=resolve;}})}};
const vm={{
  allMaterials:[],materials:[],materialSupplierFilter:"",materialLayerFilter:3,compareKeyword:"",compareWeight:"",compareGroups:[{{name:"旧"}}],compareLoading:false,compareError:"",showCompareModal:true,
  compareRequestKey(){{return JSON.stringify({{supplier_name:null,layer_count:3,keyword:this.compareKeyword,weight:this.compareWeight}});}},
  beginLatestRequest(key){{const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},errorMessage(error){{return error?.message||String(error);}},
  invalidateCompareResults(){{const key="materials:compare";const controller=global.latestRequestControllers.get(key);controller?.abort();if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);this.compareLoading=false;this.compareGroups=[];this.compareError=this.showCompareModal?"比价条件已变化，请重新查询。":"";}}
}};
vm.closeCompare=new Function({json.dumps(close_body, ensure_ascii=False)}).bind(vm);
vm.loadCompare=new AsyncFunction({json.dumps(load_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const request=vm.loadCompare();
  expect(vm.closeCompare()===true&&vm.showCompareModal===false,"compare modal did not close");
  release({{data:{{groups:[{{name:"迟到结果"}}]}}}});
  expect(await request===false,"closed compare accepted a late response");
  expect(vm.compareGroups.length===0&&vm.compareLoading===false,"closed compare retained stale state");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "material-compare-close.js")


def test_compare_request_preserves_existing_business_filters() -> None:
    load_body = _method_body("async loadCompare() {", "compareFluteOptions() {")
    assert "supplier_name:" in load_body
    assert "layer_count:" in load_body
    assert "material_id:" in load_body
    assert "basis_weight_description:" in load_body
    assert "flute_type: null" in load_body
    assert '"/api/master/materials/compare"' in load_body

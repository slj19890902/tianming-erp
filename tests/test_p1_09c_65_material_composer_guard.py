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
    assert node is not None, "Node.js is required for the material-composer regression"
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


def test_material_composer_ui_has_local_parse_and_save_feedback() -> None:
    assert "materialComposerPreviewLoading: false" in INDEX
    assert "materialComposerSavePending: false" in INDEX
    assert "materialComposerPreviewLoading ? '解析中…' : '解析'" in INDEX
    assert ':disabled="materialComposerSavePending || !materialComposerCanSave"' in INDEX
    assert "materialComposerSavePending ? '保存中…' : '保存为可用材质'" in INDEX
    assert ':disabled="loading || !materialComposerCanSave"' not in INDEX


def test_material_composer_preview_keeps_only_latest_input(tmp_path: Path) -> None:
    body = _method_body("async previewMaterialComposition() {", "async saveMaterialComposition() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];const notices=[];
global.axios={{post:(url,payload,options)=>new Promise((resolve,reject)=>pending.push({{url,payload,options,resolve,reject}}))}};
const vm={{
  materialComposer:{{supplier_name:"苏州嘉林亿",layer_count:3,material_code:"ABC",usage_flute_type:"",quote_price:"",remarks:""}},
  materialComposerPreview:null,materialComposerParseKey:"",materialComposerInvalidReason:"",materialComposerPriceSource:"",materialComposerPreviewLoading:false,
  materialComposerInputKey(row=this.materialComposer){{return `${{String(row.supplier_name||"").trim()}}|${{Number(row.layer_count||0)}}|${{String(row.material_code||"").trim().toUpperCase()}}|${{String(row.usage_flute_type||"").trim().toUpperCase()}}`; }},
  beginLatestRequest(key){{latestRequestControllers.get(key)?.abort();const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}},showToast(message,error=false){{notices.push([message,error]);}}
}};
vm.previewMaterialComposition=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.previewMaterialComposition();
  vm.materialComposer.material_code="A+A";
  const second=vm.previewMaterialComposition();
  pending[1].resolve({{data:{{valid:true,duplicate_material:false,current_suggested_price:1.23,parse_key:"苏州嘉林亿|3|A+A",supplier_name:"苏州嘉林亿",layer_count:3,material_code:"A+A",usage_flute_type:null}}}});
  expect(await second===true,"latest preview did not report success");
  expect(vm.materialComposerPreview.material_code==="A+A"&&vm.materialComposer.material_code==="A+A","latest preview was not applied");
  pending[0].resolve({{data:{{valid:true,duplicate_material:false,current_suggested_price:9.99,parse_key:"苏州嘉林亿|3|ABC",supplier_name:"苏州嘉林亿",layer_count:3,material_code:"ABC",usage_flute_type:null}}}});
  expect(await first===false,"old preview was treated as current");
  expect(vm.materialComposerPreview.material_code==="A+A"&&vm.materialComposer.material_code==="A+A","old preview overwrote latest input/result");

  vm.materialComposer.material_code="AAA";vm.materialComposerPreview=null;vm.materialComposerParseKey="";
  const stale=vm.previewMaterialComposition();
  vm.materialComposer.material_code="BBB";
  pending[2].resolve({{data:{{valid:true,duplicate_material:false,current_suggested_price:2,parse_key:"苏州嘉林亿|3|AAA",supplier_name:"苏州嘉林亿",layer_count:3,material_code:"AAA",usage_flute_type:null}}}});
  expect(await stale===false,"response for changed input was applied");
  expect(vm.materialComposer.material_code==="BBB"&&vm.materialComposerPreview===null,"stale response restored an obsolete preview");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "material-composer-latest-preview.js")


def test_material_composer_preview_declares_latest_request_contract() -> None:
    body = _method_body("async previewMaterialComposition() {", "async saveMaterialComposition() {")
    assert 'const requestKey = "materials:compose-preview";' in body
    assert "const requestedInputKey = this.materialComposerInputKey(requested);" in body
    assert "signal:controller.signal" in body
    assert "latestRequestControllers.get(requestKey) !== controller" in body
    assert "this.materialComposerInputKey() !== requestedInputKey" in body
    assert "this.materialComposer.material_code = code" not in body


def test_material_composer_save_blocks_duplicates_freezes_payload_and_handles_refresh(tmp_path: Path) -> None:
    body = _method_body("async saveMaterialComposition() {", "recommendMaterialCode() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let writes=0,release,closed=0;const notices=[];
global.axios={{post:(url,payload)=>new Promise(resolve=>{{writes+=1;global.saved={{url,payload}};release=resolve;}})}};
const vm={{
  materialComposer:{{supplier_name:"苏州嘉林亿",layer_count:3,material_code:"A+A",usage_flute_type:"",quote_price:"1.25",remarks:"首版"}},
  materialComposerPreview:{{valid:true,duplicate_material:false,supplier_name:"苏州嘉林亿",layer_count:3,material_code:"A+A",usage_flute_type:null}},
  materialComposerPriceSource:"manual",materialComposerCanSave:true,materialComposerSavePending:false,allMaterials:[1],materialSuppliers:["苏州嘉林亿"],
  loadMaterials:async()=>false,closeMaterialComposer(){{closed+=1;}},
  showToast(message,error=false){{notices.push([message,error]);}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.saveMaterialComposition=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveMaterialComposition();
  vm.materialComposer={{supplier_name:"其他",layer_count:5,material_code:"ABCDE",usage_flute_type:"",quote_price:"9",remarks:"被改动"}};
  vm.materialComposerPreview={{supplier_name:"其他",layer_count:5,material_code:"ABCDE",usage_flute_type:null}};
  const duplicate=await vm.saveMaterialComposition();
  expect(duplicate===false&&writes===1,"double save issued another mutation");
  expect(global.saved.payload.supplier_name==="苏州嘉林亿"&&global.saved.payload.material_code==="A+A"&&global.saved.payload.quote_price===1.25&&global.saved.payload.remarks==="首版","save payload was not frozen at click time");
  release({{data:{{message:"已保存"}}}});expect(await first===true,"committed write plus refresh failure was reported as mutation failure");
  expect(closed===1&&vm.materialComposerSavePending===false,"successful write did not close/unlock composer");
  expect(notices.some(([message,error])=>message.includes("已保存")&&message.includes("列表刷新失败")&&message.includes("不要重复保存")&&error===true),"safe refresh-failure guidance missing");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "material-composer-save-guard.js")

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


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the material lifecycle regression"
    target = tmp_path / "material-lifecycle-guard.js"
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


def test_material_deactivate_button_uses_truthful_copy_and_feedback() -> None:
    assert 'materialLifecycleAction:{action:"",materialId:null}' in INDEX
    assert ':disabled="materialLifecycleBusy()" @click="deleteMaterial(row)"' in INDEX
    assert "materialLifecyclePending('deactivate',row.id) ? '处理中…' : '停用'" in INDEX
    assert '@click="deleteMaterial(row)">删除</button>' not in INDEX


def test_material_lifecycle_runtime_blocks_duplicates_and_releases(tmp_path: Path) -> None:
    body = _method_body(
        "async runMaterialLifecycleAction({action, materialId=null, confirmMessage, task}) {",
        "async deleteMaterial(row) {",
    )
    script = f"""
const body={json.dumps(body, ensure_ascii=False)};
const factory=new Function("return async function({{action, materialId=null, confirmMessage, task}}) {{"+body+"}}");
let confirmCount=0,taskCount=0,toastCount=0,allowConfirm=true,release;
global.confirm=()=>{{confirmCount+=1;return allowConfirm;}};
const vm={{
  materialLifecycleAction:{{action:"",materialId:null}},
  materialLifecycleBusy(){{return Boolean(this.materialLifecycleAction?.action);}},
  showToast(){{toastCount+=1;}},
  errorMessage(error){{return error?.message||String(error);}}
}};
vm.runMaterialLifecycleAction=factory().bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.runMaterialLifecycleAction({{action:"deactivate",materialId:7,confirmMessage:"确认",task:()=>{{taskCount+=1;return new Promise(resolve=>{{release=resolve;}});}}}});
  const duplicate=await vm.runMaterialLifecycleAction({{action:"deactivate",materialId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(duplicate===false,"duplicate lifecycle action was not rejected");
  expect(confirmCount===1&&taskCount===1,"duplicate action repeated confirmation or request");
  release(true);expect(await first===true,"first lifecycle action did not complete");
  expect(vm.materialLifecycleAction.action==="","success did not release lifecycle lock");
  const failed=await vm.runMaterialLifecycleAction({{action:"deactivate",materialId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;throw new Error("失败");}}}});
  expect(failed===false&&toastCount===1,"failure was not reported once");
  expect(vm.materialLifecycleAction.action==="","failure did not release lifecycle lock");
  allowConfirm=false;
  const cancelled=await vm.runMaterialLifecycleAction({{action:"deactivate",materialId:7,confirmMessage:"确认",task:async()=>{{taskCount+=1;return true;}}}});
  expect(cancelled===false&&taskCount===2,"cancelled confirmation still executed mutation");
  expect(vm.materialLifecycleAction.action==="","cancelled confirmation acquired lifecycle lock");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_delete_material_uses_shared_guard_and_keeps_versioned_mutation() -> None:
    body = _method_body("async deleteMaterial(row) {", "async openOrder() {")

    assert "this.runMaterialLifecycleAction({" in body
    assert 'action:"deactivate"' in body
    assert "this.sendVersionedMasterMutation({" in body
    assert 'method:"delete"' in body
    assert "this.loadMaterials()" in body
    assert 'this.showToast("材质已停用")' in body

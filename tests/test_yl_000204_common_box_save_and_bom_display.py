"""Regressions for standalone GRAPH roots and unlisted common-box fields."""

from pathlib import Path
import json
import re
import shutil
import subprocess


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def _method(name: str) -> str:
    start = re.search(r"^          (?:async )?" + re.escape(name) + r"\(", INDEX, re.M)
    assert start, name
    end = re.search(r"^          (?:async )?[A-Za-z_$][\w$]*\(", INDEX[start.end() :], re.M)
    assert end, name
    return INDEX[start.start() : start.end() + end.start()]


def _node(script: str) -> None:
    result = subprocess.run([shutil.which("node") or "node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_standalone_graph_root_is_not_displayed_as_bom() -> None:
    script = "const assert=require('node:assert/strict');\n"
    script += "const vm={" + _method("visibleBomComponents") + _method("hasBomComponents") + "};\n"
    script += """
const standalone={product_id:3494,bom_components:[{
  id:56,component_product_id:3494,product_bom_component_id:null,
  internal_component_code:'GRAPH-3494'
}]};
assert.equal(vm.hasBomComponents(standalone),false);
assert.deepEqual(vm.visibleBomComponents(standalone),[]);
const child={id:57,component_product_id:4000,product_bom_component_id:12,
  internal_component_code:'YL-CHILD'};
const composite={...standalone,bom_components:[...standalone.bom_components,child]};
assert.equal(vm.hasBomComponents(composite),true);
assert.deepEqual(vm.visibleBomComponents(composite),composite.bom_components);
assert.equal(vm.hasBomComponents({product_id:3494,bom_components:[]}),false);
"""
    _node(script)
    assert INDEX.count('v-for="component in visibleBomComponents(item)"') == 3


def test_unlisted_common_box_change_enters_product_preflight() -> None:
    body = INDEX.split("async saveModal() {", 1)[1].split("async confirmStockExternalDraft(row) {", 1)[0].rsplit("}", 1)[0]
    script = "const assert=require('node:assert/strict');\n"
    script += "const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;\n"
    script += "const vm={modal:{type:'product'},bomEditor:{loading:false,error:''},productForm:{box_style:'A1/0201 普通开槽箱'},masterSavePending:false,masterPendingSaveOptions:null,loading:false,"
    script += "masterCurrentForm(){return {id:3494}},masterLocalChanges(){return []},_productFormDirty(){return true},"
    script += "_productBomDirty(){return false},masterCurrentVersion(){return 11},"
    script += "prepareProductOneClickSave(){this.previewCount++;return Promise.resolve({completed:true})},"
    script += "previewCount:0,closeModal(){this.closed=true},showToast(msg){this.toast=msg}};\n"
    script += "vm.saveModal=new AsyncFunction(" + json.dumps(body, ensure_ascii=False) + ").bind(vm);\n"
    script += "(async()=>{assert.equal(await vm.saveModal(),true);assert.equal(vm.previewCount,1);"
    script += "assert.equal(vm.masterSavePending,false);assert.equal(vm.closed,undefined);"
    script += "assert.equal(vm.toast,undefined)})().catch(e=>{console.error(e);process.exit(1)});"
    _node(script)


def test_main_common_box_save_adopts_changed_default_location() -> None:
    body = INDEX.split("async saveModal() {", 1)[1].split("async confirmStockExternalDraft(row) {", 1)[0].rsplit("}", 1)[0]
    script = "const assert=require('node:assert/strict');\n"
    script += "const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;\n"
    script += "const methods={" + _method("receiptStorageDirty") + "};\n"
    script += "const vm={...methods,modal:{type:'product'},bomEditor:{loading:false,error:''},"
    script += "productForm:{id:3494,box_style:'A1/0201 普通开槽箱'},masterSavePending:false,"
    script += "masterPendingSaveOptions:null,receiptStorage:{product_id:3494,floor:2,saved_floor:1,"
    script += "area_id:8,saved_area_id:7,location_id:1620,saved_location_id:1300},"
    script += "masterCurrentForm(){return this.productForm},masterLocalChanges(){return []},"
    script += "_productFormDirty(){return false},_productBomDirty(){return false},"
    script += "saveReceiptStorage(clear){this.storageClear=clear;this.storageWrites++;return Promise.resolve(true)},"
    script += "storageWrites:0,closeModal(){this.closed=true},showToast(msg){this.toast=msg}};\n"
    script += "vm.saveModal=new AsyncFunction(" + json.dumps(body, ensure_ascii=False) + ").bind(vm);\n"
    script += "(async()=>{assert.equal(await vm.saveModal(),true);assert.equal(vm.storageWrites,1);"
    script += "assert.equal(vm.storageClear,false);assert.equal(vm.closed,true);"
    script += "assert.equal(vm.toast,'默认位置已保存');assert.equal(vm.masterSavePending,false)})()"
    script += ".catch(e=>{console.error(e);process.exit(1)});"
    _node(script)


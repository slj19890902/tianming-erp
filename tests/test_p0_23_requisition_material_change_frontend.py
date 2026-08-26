from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_requisition_material_modal_uses_one_cached_option_projection() -> None:
    modal_marker = '<div v-else-if="modal.type === \'requisitionMaterial\'">'
    start = INDEX.index(modal_marker)
    end = INDEX.index('<div v-else-if="modal.type === \'mobileEntry\'"', start)
    modal = INDEX[start:end]

    assert ':options="requisitionMaterialOptions"' in modal
    assert (
        ':options="materialSelectOptions(requisitionMaterialForm.supplier_name,'
        not in modal
    )
    assert "requisitionMaterialOptions()" in INDEX
    assert "filtered.slice(0, 50)" in INDEX
    assert 'Vue.markRaw(projected)' in INDEX
    assert '@search="requisitionMaterialSearch=$event"' in modal


def test_search_select_does_not_write_the_same_label_on_every_option_refresh() -> None:
    assert (
        'options() { if (!this.open && this.query !== this.selectedLabel) '
        'this.query=this.selectedLabel; }'
    ) in INDEX


def test_requisition_material_projection_caps_large_master_and_keeps_selection(
    tmp_path: Path,
) -> None:
    body = _function_body(
        INDEX,
        "requisitionMaterialOptions() {",
        "// 列表展示：",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
global.Vue={{markRaw:value=>value}};
const rows=Array.from({{length:630}},(_,index)=>({{id:index+1,code:`M${{index+1}}`}}));
const vm={{
  requisitionMaterialForm:{{supplier_name:"YL",layer_count:5,flute_type:"AB",material_id:630}},
  requisitionMaterialSearch:"",
  filteredMaterialOptions(supplier,layer,flute,keyword){{
    return keyword ? rows.filter(row=>row.code.includes(keyword)) : rows;
  }},
  materialSelectOption(row){{return {{...row,_label:row.code}};}}
}};
const project=new Function({json.dumps(body, ensure_ascii=False)});
const initial=project.call(vm);
if(initial.length!==50)throw new Error(`expected 50 visible options, got ${{initial.length}}`);
if(!initial.some(row=>row.id===630))throw new Error("selected material was dropped");
vm.requisitionMaterialSearch="M62";
const searched=project.call(vm);
if(searched.length>50||!searched.every(row=>row.code.includes("M62")))throw new Error("search projection escaped its bounded filtered result");
"""
    target = tmp_path / "p0-23-requisition-material-projection.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _function_body(source: str, signature: str, next_signature: str) -> str:
    assert signature in source and next_signature in source
    return source.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def test_order_quantity_can_be_received_before_optional_reserve_has_a_location(
    tmp_path: Path,
) -> None:
    desktop_body = _function_body(
        INDEX,
        "incomingReceiptExecutionIssue(row) {",
        "toggleAllIncoming(checked) {",
    )
    mobile = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")
    mobile_body = _function_body(
        mobile,
        "function receiptExecutionIssue(item) {",
        "function isPdfPath(path) {",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const FunctionCtor=Function;
const desktop=new FunctionCtor("row",{json.dumps(desktop_body, ensure_ascii=False)});
const mobile=new FunctionCtor("item",{json.dumps(mobile_body, ensure_ascii=False)});
const row={{
  incoming_quantity:602,
  expected_order_purpose_sheet_qty:600,
  expected_reserve_purpose_sheet_qty:2,
  remaining_order_purpose_sheet_qty:600,
  remaining_reserve_purpose_sheet_qty:2,
  receipt_execution_ready:false,
  reserve_location_ready:false,
  finished_location_ready:true,
  purpose_issue:"一楼原料区域尚无已发布真实排位",
}};
function expect(value,message){{if(!value)throw new Error(message);}}
expect(desktop(row).includes("一楼原料"),"desktop allowed reserve sheets without a location");
expect(mobile(row).includes("一楼原料"),"mobile allowed reserve sheets without a location");
row.incoming_quantity=600;
expect(desktop(row)==="","desktop blocked the order-purpose quantity");
expect(mobile(row)==="","mobile blocked the order-purpose quantity");
"""
    target = tmp_path / "p0-23-order-purpose-receipt.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

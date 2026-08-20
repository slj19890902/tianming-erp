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
    assert node is not None, "Node.js is required for requisition preview regression"
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


def _runtime(open_body: str, signature_body: str, cancel_body: str) -> str:
    return f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];const toasts=[];
global.axios={{post:(url,payload,options)=>new Promise((resolve,reject)=>pending.push({{url,payload,options,resolve,reject}}))}};
let currentRows=[];
const vm={{
  canRequisition:true,activePage:"requisition",supplierRequisitionPreviewLoading:false,
  supplierRequisitionSelections:[],supplierRequisitionDraft:{{supplier_groups:[]}},modal:null,
  selectedPendingRows(){{return currentRows;}},isPendingRowSelectable(){{return true;}},
  pendingSupplierSelectionPayload(row){{return {{type:"order_item",order_item_id:row.id,supplier_name:row.supplier_name,report_length_mm:row.length,report_width_mm:row.width,cutting_mode:row.cutting_mode,remark:row.remark||null}};}},
  initializeSupplierPurchasePurposeDraft(data){{return data;}},
  openCompositeRequisition:async()=>true,supplierDraftLateFinishedSources:()=>[],supplierDraftSemiInventoryOptions:()=>[],
  beginLatestRequest(key){{global.latestRequestControllers.get(key)?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="CanceledError";}},showToast(message,isError){{toasts.push({{message,isError}});}}
}};
vm.supplierRequisitionSelectionSignature=new Function("selections",{json.dumps(signature_body, ensure_ascii=False)}).bind(vm);
vm.cancelSupplierRequisitionPreview=new Function({json.dumps(cancel_body, ensure_ascii=False)}).bind(vm);
vm.openSupplierRequisitionDraft=new AsyncFunction("rows",{json.dumps(open_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
"""


def test_merge_button_has_single_flight_loading_feedback() -> None:
    click = INDEX.index('@click="openSupplierRequisitionDraft()"')
    start = INDEX.rfind("<button", 0, click)
    end = INDEX.index("</button>", click) + len("</button>")
    button = INDEX[start:end]

    assert ':disabled="supplierRequisitionPreviewLoading ||' in button
    assert "正在生成草稿…" in button
    assert "合并报料" in button


def test_latest_requisition_preview_response_wins(tmp_path: Path) -> None:
    signature = _method_body(
        "supplierRequisitionSelectionSignature(selections) {",
        "cancelSupplierRequisitionPreview() {",
    )
    cancel = _method_body(
        "cancelSupplierRequisitionPreview() {",
        "async openSupplierRequisitionDraft(rows = null) {",
    )
    opened = _method_body(
        "async openSupplierRequisitionDraft(rows = null) {",
        "async openCompositeRequisition(rows) {",
    )
    script = _runtime(opened, signature, cancel) + """
(async()=>{
  const rowA={id:1,supplier_name:"A",length:100,width:200,cutting_mode:"一开一"};
  const rowB={id:2,supplier_name:"B",length:300,width:400,cutting_mode:"一开二"};
  currentRows=[rowA];const first=vm.openSupplierRequisitionDraft();
  currentRows=[rowB];const second=vm.openSupplierRequisitionDraft();
  pending[1].resolve({data:{supplier_groups:[{supplier_name:"B"}]}});
  expect(await second===true,"latest preview did not complete");
  pending[0].resolve({data:{supplier_groups:[{supplier_name:"A"}]}});
  expect(await first===false,"stale preview response was accepted");
  expect(vm.supplierRequisitionDraft.supplier_groups[0].supplier_name==="B","stale preview replaced latest draft");
  expect(vm.supplierRequisitionSelections[0].order_item_id===2&&vm.modal?.type==="supplierRequisitionDraft","latest selection was not retained");
  expect(toasts.length===0&&!vm.supplierRequisitionPreviewLoading,"stale preview leaked feedback or loading state");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "requisition-preview-latest.js")


def test_changed_selection_blocks_old_preview_without_opening(tmp_path: Path) -> None:
    signature = _method_body(
        "supplierRequisitionSelectionSignature(selections) {",
        "cancelSupplierRequisitionPreview() {",
    )
    cancel = _method_body(
        "cancelSupplierRequisitionPreview() {",
        "async openSupplierRequisitionDraft(rows = null) {",
    )
    opened = _method_body(
        "async openSupplierRequisitionDraft(rows = null) {",
        "async openCompositeRequisition(rows) {",
    )
    script = _runtime(opened, signature, cancel) + """
(async()=>{
  currentRows=[{id:1,supplier_name:"A",length:100,width:200,cutting_mode:"一开一"}];
  const request=vm.openSupplierRequisitionDraft();
  currentRows=[{id:1,supplier_name:"A",length:100,width:260,cutting_mode:"一开二"}];
  pending[0].resolve({data:{supplier_groups:[{supplier_name:"A"}]}});
  expect(await request===false,"changed selection preview was accepted");
  expect(vm.modal===null&&vm.supplierRequisitionDraft.supplier_groups.length===0,"old preview opened after inline values changed");
  expect(toasts.length===1&&toasts[0].message.includes("选择或参数已变化"),"operator did not receive a clear retry message");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "requisition-preview-changed.js")


def test_stale_error_and_leave_page_cannot_open_preview(tmp_path: Path) -> None:
    signature = _method_body(
        "supplierRequisitionSelectionSignature(selections) {",
        "cancelSupplierRequisitionPreview() {",
    )
    cancel = _method_body(
        "cancelSupplierRequisitionPreview() {",
        "async openSupplierRequisitionDraft(rows = null) {",
    )
    opened = _method_body(
        "async openSupplierRequisitionDraft(rows = null) {",
        "async openCompositeRequisition(rows) {",
    )
    script = _runtime(opened, signature, cancel) + """
(async()=>{
  currentRows=[{id:1,supplier_name:"A",length:100,width:200,cutting_mode:"一开一"}];
  const request=vm.openSupplierRequisitionDraft();
  vm.activePage="orders";vm.cancelSupplierRequisitionPreview();
  const staleError=new Error("旧预览失败");staleError.name="CanceledError";pending[0].reject(staleError);
  expect(await request===false,"cancelled preview was accepted");
  expect(vm.modal===null&&toasts.length===0&&!vm.supplierRequisitionPreviewLoading,"cancelled preview leaked UI state");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "requisition-preview-leave.js")


def test_navigation_away_cancels_pending_preview() -> None:
    go = _method_body("async go(page) {", "isMenuActive(menuKey) {")
    reset = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")
    assert 'if (page !== "requisition") this.cancelSupplierRequisitionPreview();' in go
    assert "this.supplierRequisitionPreviewLoading = false" in reset


def test_preview_remains_non_persisting_and_uses_frozen_snapshot() -> None:
    opened = _method_body(
        "async openSupplierRequisitionDraft(rows = null) {",
        "async openCompositeRequisition(rows) {",
    )
    assert "JSON.parse(JSON.stringify" in opened
    assert "axios.post(" in opened
    assert '"/api/requisition/supplier-orders/preview-from-pending-selection"' in opened
    assert "signal:controller.signal" in opened
    assert "from-pending-selection" not in opened.replace("preview-from-pending-selection", "")
    assert "/api/requisition/batches" not in opened

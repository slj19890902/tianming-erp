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
    assert node is not None, "Node.js is required for composite requisition regression"
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


def test_composite_formal_button_has_dedicated_operation_state() -> None:
    assert (
        "compositeRequisitionSaveState:{saving:false, committed:false, uncertain:false, result:null, requestKey:\"\", draftSignature:\"\"}"
        in INDEX
    )
    click = INDEX.index("modal?.type==='delivery' ? deliveryPrimaryAction() : saveModal()")
    start = INDEX.rfind("<button", 0, click)
    end = INDEX.index("</button>", click) + len("</button>")
    button = INDEX[start:end]
    assert "compositeRequisitionSaveState.saving" in button
    assert "compositeRequisitionSaveState.committed" in button
    assert "正在生成组合报料…" in button
    assert "组合报料已生成" in button


def test_composite_formal_save_freezes_payload_and_commits_before_refresh() -> None:
    block = _method_body(
        "async saveCompositeRequisitionDraft() {",
        "async saveStockReplenishmentDraft() {",
    )
    assert "if (state.saving || state.committed)" in block
    assert "const frozenPayload = JSON.parse(JSON.stringify(payload))" in block
    assert 'axios.post("/api/requisition/batches", frozenPayload)' in block
    assert block.index("state.committed = true") < block.index("Promise.allSettled")
    assert "_refresh_failed:refreshFailed" in block
    assert "_print_failed:printFailed" in block


def _runtime(block: str) -> str:
    return f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const pending=[];const toasts=[];const printCalls=[];
global.createIdempotencyKey=()=>"p1-09c-76-stable-key";
global.axios={{post:(url,payload)=>new Promise((resolve,reject)=>pending.push({{url,payload,resolve,reject}}))}};
global.window={{open:(url,target)=>{{printCalls.push({{url,target}});return {{}};}}}};
const vm={{
  compositeRequisitionSaveState:{{saving:false,committed:false,uncertain:false,result:null,requestKey:"",draftSignature:""}},
  requisitionForm:{{supplier_name:"鸣朋",items:[{{order_item_id:1,component_type:"whole",bom_snapshot_id:3,requisition_qty:10,cardboard_len:800,cardboard_width:600,special_process:"一开二",remark:""}}]}},
  requisitionSelected:{{a:true}},selectedPendingKeys:["a"],selectedBomSnapshotIds:["3:whole"],modal:{{type:"requisition"}},
  validateRequisitionForm(){{return "";}},requisitionBatchLinePayloads(line){{return [{{...line}}];}},
  loadRequisition:async()=>true,showToast(message,isError){{toasts.push({{message,isError}});}}
}};
vm.saveCompositeRequisitionDraft=new AsyncFunction({json.dumps(block, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
"""


def test_composite_same_draft_is_single_flight_and_committed_result_is_reused(
    tmp_path: Path,
) -> None:
    block = _method_body(
        "async saveCompositeRequisitionDraft() {",
        "async saveStockReplenishmentDraft() {",
    )
    script = _runtime(block) + """
(async()=>{
  const first=vm.saveCompositeRequisitionDraft();
  const secondPromise=vm.saveCompositeRequisitionDraft();
  await Promise.resolve();
  expect(pending.length===1,"double submit sent more than one batch POST");
  const second=await secondPromise;
  expect(second?._in_flight===true,"second call was not marked in flight");
  pending[0].resolve({data:{id:31,requisition_number:"BL-31"}});
  const result=await first;
  expect(result.id===31&&vm.compositeRequisitionSaveState.committed,"successful batch was not committed in UI state");
  const replay=await vm.saveCompositeRequisitionDraft();
  expect(pending.length===1&&replay.id===31,"committed draft was posted again");
  expect(printCalls.length===1&&printCalls[0].url==="/requisition-print.html?id=31","formal print was not opened exactly once");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "composite-requisition-single-flight.js")


def test_composite_success_survives_refresh_and_print_failures(tmp_path: Path) -> None:
    block = _method_body(
        "async saveCompositeRequisitionDraft() {",
        "async saveStockReplenishmentDraft() {",
    )
    script = _runtime(block) + """
(async()=>{
  vm.loadRequisition=async()=>{throw new Error("刷新失败");};
  window.open=()=>null;
  const request=vm.saveCompositeRequisitionDraft();
  pending[0].resolve({data:{id:32,requisition_number:"BL-32"}});
  const result=await request;
  expect(result._refresh_failed===true&&result._print_failed===true,"post-success auxiliary failures were hidden");
  expect(vm.compositeRequisitionSaveState.committed===true&&vm.modal===null,"successful draft remained resubmittable");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "composite-requisition-success-boundary.js")


def test_composite_network_unknown_closes_original_draft() -> None:
    assert "_compositeRequisitionOutcomeUncertain" in INDEX
    assert "组合报料提交连接中断，结果暂不确定" in INDEX
    assert "不要重复点击原草稿" in INDEX


def test_backend_still_rejects_duplicate_composite_sources() -> None:
    source = (ROOT / "app" / "api" / "requisition.py").read_text(encoding="utf-8")
    assert "同一复合产品物理料不能重复报料" in source
    assert "该复合产品物理料订单用途已报足，不能重复创建" in source
    assert "db.rollback()" in source

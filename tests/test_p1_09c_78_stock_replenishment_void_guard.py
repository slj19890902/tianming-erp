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
    assert node is not None, "Node.js is required for replenishment void regression"
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


def test_replenishment_void_button_has_dedicated_operation_state() -> None:
    assert (
        "stockReplenishmentVoidState:{orderId:null, documentNumber:\"\", uncertainIds:{}}"
        in INDEX
    )
    hit = INDEX.index('@click="voidReportedReplenishment(row)"')
    start = INDEX.rfind("<button", 0, hit)
    end = INDEX.index("</button>", hit) + len("</button>")
    button = INDEX[start:end]
    assert "stockReplenishmentVoidBusy(row)" in button
    assert "stockReplenishmentVoidUncertain(row)" in button
    assert "撤销中…" in button
    assert "请刷新核对" in button


def test_replenishment_void_freezes_target_and_marks_success_before_refresh() -> None:
    block = _method_body(
        "async voidReportedReplenishment(row) {",
        "async voidReportedCompositeRequisition(row) {",
    )
    assert "const orderId = Number(row.id)" in block
    assert "const documentNumber = String(row.document_number" in block
    assert 'axios.put("/api/requisition/stock-replenishment/orders/"+orderId+"/void")' in block
    assert block.index('row.status = "voided"') < block.index("Promise.allSettled")
    assert "row.can_void = false" in block
    assert "_refresh_failed:refreshFailed" in block


def _runtime(block: str) -> str:
    return f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const pending=[];const messages=[];
global.confirm=()=>true;
global.axios={{put:(url)=>new Promise((resolve,reject)=>pending.push({{url,resolve,reject}}))}};
const vm={{
  stockReplenishmentVoidState:{{orderId:null,documentNumber:"",uncertainIds:{{}}}},
  loadRequisition:async()=>true,loadReportedDocuments:async()=>true,
  showToast(message,isError){{messages.push({{message,isError}});}},
  errorMessage(error){{return error?.message||"error";}},
}};
vm.stockReplenishmentVoidBusy=function(row){{return Number(this.stockReplenishmentVoidState.orderId)===Number(row?.id);}};
vm.stockReplenishmentVoidUncertain=function(row){{return !!this.stockReplenishmentVoidState.uncertainIds[Number(row?.id)];}};
vm.voidReportedReplenishment=new AsyncFunction("row",{json.dumps(block, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
"""


def test_replenishment_void_is_single_flight_and_target_is_frozen(tmp_path: Path) -> None:
    block = _method_body(
        "async voidReportedReplenishment(row) {",
        "async voidReportedCompositeRequisition(row) {",
    )
    script = _runtime(block) + """
(async()=>{
  const row={id:12,document_number:"CBR-12",status:"confirmed",can_void:true,incoming_status:"待入库"};
  const first=vm.voidReportedReplenishment(row);
  const second=await vm.voidReportedReplenishment(row);
  await Promise.resolve();
  expect(second===false&&pending.length===1,"double click sent more than one PUT");
  row.id=99;row.document_number="CHANGED";
  expect(pending[0].url.endsWith("/12/void"),"request target changed after click");
  pending[0].resolve({data:{id:12,status:"voided"}});
  const result=await first;
  expect(result.id===12&&row.status==="voided"&&row.can_void===false,"successful void was not committed locally");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "stock-replenishment-void-single-flight.js")


def test_void_success_survives_refresh_failure_and_network_unknown_is_locked(
    tmp_path: Path,
) -> None:
    block = _method_body(
        "async voidReportedReplenishment(row) {",
        "async voidReportedCompositeRequisition(row) {",
    )
    script = _runtime(block) + """
(async()=>{
  const saved={id:21,document_number:"CBR-21",status:"confirmed",can_void:true,incoming_status:"待入库"};
  vm.loadRequisition=async()=>{throw new Error("刷新失败");};
  vm.loadReportedDocuments=async()=>false;
  const request=vm.voidReportedReplenishment(saved);
  pending[0].resolve({data:{id:21,status:"voided"}});
  const result=await request;
  expect(result._refresh_failed===true,"refresh failure hid successful void");
  expect(saved.status==="voided"&&saved.can_void===false,"successful row remained actionable");
  expect(messages.some(item=>item.message.includes("已经撤销")&&item.message.includes("刷新失败")),"success warning missing");

  const unknown={id:22,document_number:"CBR-22",status:"confirmed",can_void:true,incoming_status:"待入库"};
  vm.loadRequisition=async()=>true;vm.loadReportedDocuments=async()=>true;
  const unknownRequest=vm.voidReportedReplenishment(unknown);
  pending[1].reject(new Error("连接中断"));
  const unknownResult=await unknownRequest;
  expect(unknownResult===false&&vm.stockReplenishmentVoidUncertain(unknown),"unknown result was not locked for refresh");
  expect(unknown.can_void===true&&unknown.status==="confirmed","unknown result fabricated a void success");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "stock-replenishment-void-outcomes.js")


def test_known_4xx_void_failure_can_retry(tmp_path: Path) -> None:
    block = _method_body(
        "async voidReportedReplenishment(row) {",
        "async voidReportedCompositeRequisition(row) {",
    )
    script = _runtime(block) + """
(async()=>{
  const row={id:31,document_number:"CBR-31",status:"confirmed",can_void:true,incoming_status:"待入库"};
  const request=vm.voidReportedReplenishment(row);
  const error=new Error("已经收货");error.response={status:409,data:{detail:"已经收货"}};
  pending[0].reject(error);
  const result=await request;
  expect(result===false&&!vm.stockReplenishmentVoidUncertain(row),"known 4xx was locked as unknown");
  expect(row.can_void===true&&row.status==="confirmed","known 4xx changed local business state");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "stock-replenishment-void-known-failure.js")


def test_backend_uses_atomic_void_and_receipt_claim_contracts() -> None:
    api = (ROOT / "app" / "api" / "requisition.py").read_text(encoding="utf-8")
    void_block = api.split("def void_stock_replenishment_order(", 1)[1].split(
        '@router.get("/historical-purchases/search")', 1
    )[0]
    assert "update(StockReplenishmentOrder)" in void_block
    assert 'StockReplenishmentOrder.status == "confirmed"' in void_block
    assert 'values(status="voided", voided_at=voided_at)' in void_block
    assert "if transition.rowcount != 1:" in void_block

    incoming = (ROOT / "app" / "services" / "incoming_receipts.py").read_text(
        encoding="utf-8"
    )
    target_block = incoming.split("def _stock_target(", 1)[1].split(
        "def _stock_source_filter", 1
    )[0]
    assert "claim_for_receipt: bool = False" in target_block
    assert "update(StockReplenishmentOrder)" in target_block
    assert "if claim.rowcount != 1:" in target_block
    assert "db.expire_all()" in target_block

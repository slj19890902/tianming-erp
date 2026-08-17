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
    assert node is not None, "Node.js is required for requisition save regression"
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


def test_formal_supplier_order_button_has_business_operation_state() -> None:
    assert (
        "supplierRequisitionSaveState:{saving:false, committed:false, uncertain:false, result:null}"
        in INDEX
    )
    click = INDEX.index("modal?.type==='delivery' ? deliveryPrimaryAction() : saveModal()")
    start = INDEX.rfind("<button", 0, click)
    end = INDEX.index("</button>", click) + len("</button>")
    button = INDEX[start:end]
    assert "supplierRequisitionSaveState.saving" in button
    assert "supplierRequisitionSaveState.committed" in button
    assert "正在生成采购单…" in button
    assert "采购单已生成" in button


def test_formal_supplier_order_save_freezes_payload_and_marks_commit_before_refresh() -> None:
    block = _method_body(
        "async saveSupplierRequisitionDraft() {",
        "supplierRequisitionSelectionSignature(selections) {",
    )
    assert "if (state.saving || state.committed)" in block
    assert "const frozenPayload = JSON.parse(JSON.stringify(payload))" in block
    assert 'axios.post("/api/requisition/supplier-orders/from-pending-selection", frozenPayload)' in block
    assert block.index("state.committed = true") < block.index("Promise.allSettled")
    assert "result.status === \"rejected\" || result.value === false" in block
    assert "_refresh_failed:refreshFailed" in block


def test_a3_component_identity_survives_real_frontend_save_payload(
    tmp_path: Path,
) -> None:
    block = _method_body(
        "async saveSupplierRequisitionDraft() {",
        "supplierRequisitionSelectionSignature(selections) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let captured=null;
global.axios={{post:async(url,payload)=>{{captured={{url,payload}};return {{data:{{created_orders:[]}}}};}}}};
const vm={{
  supplierRequisitionSaveState:{{saving:false,committed:false,uncertain:false,result:null}},
  supplierRequisitionDraft:{{supplier_groups:[{{supplier_name:"苏州纸板供应商",request_key:"a3partial100sets",lines:[
    {{line_key:"cover",source_type:"normal",report_length_mm:2145,report_width_mm:1055,cutting_mode:"一开一",requisition_qty:100,source_items:[{{source_type:"order_item",order_item_id:9865,component_type:"cover",source_quantity:200,requisition_qty:200}}]}},
    {{line_key:"base",source_type:"normal",report_length_mm:2120,report_width_mm:1035,cutting_mode:"一开一",requisition_qty:100,source_items:[{{source_type:"order_item",order_item_id:9865,component_type:"base",source_quantity:200,requisition_qty:200}}]}}
  ]}}]}},
  requisitionSelected:{{td010:true}},selectedPendingKeys:["td010"],supplierRequisitionSelections:[{{type:"order_item",order_item_id:9865}}],supplierOrders:[],modal:{{type:"supplierRequisitionDraft"}},
  validateSupplierRequisitionDraft(){{return "";}},draftGroupLines(group){{return group.lines;}},
  loadRequisition:async()=>true,loadSupplierOrders:async()=>true,
  openSupplierOrderPrint(){{throw new Error("empty created_orders must not print");}},showToast(){{}}
}};
vm.saveSupplierRequisitionDraft=new AsyncFunction({json.dumps(block, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  await vm.saveSupplierRequisitionDraft();
  expect(captured?.url==="/api/requisition/supplier-orders/from-pending-selection","wrong save endpoint");
  const lines=captured.payload.supplier_groups[0].lines;
  expect(lines.length===2,"A3 cover/base lines were not preserved");
  expect(lines[0].source_items[0].component_type==="cover","cover identity was dropped");
  expect(lines[1].source_items[0].component_type==="base","base identity was dropped");
  expect(lines[0].requisition_qty===100&&lines[1].requisition_qty===100,"partial quantities changed");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-a3-component-round-trip.js")


def test_same_draft_is_single_flight_and_committed_result_is_not_reposted(tmp_path: Path) -> None:
    block = _method_body(
        "async saveSupplierRequisitionDraft() {",
        "supplierRequisitionSelectionSignature(selections) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const pending=[];const toasts=[];
global.axios={{post:(url,payload)=>new Promise((resolve,reject)=>pending.push({{url,payload,resolve,reject}}))}};
const vm={{
  supplierRequisitionSaveState:{{saving:false,committed:false,uncertain:false,result:null}},
  supplierRequisitionDraft:{{supplier_groups:[{{supplier_name:"鸣朋",request_key:"1234567890abcdef",lines:[{{line_key:"line-1",source_type:"order_item",report_length_mm:800,report_width_mm:600,cutting_mode:"一开一",requisition_qty:10,source_items:[{{source_type:"order_item",order_item_id:1,source_quantity:10,requisition_qty:10}}]}}]}}]}},
  requisitionSelected:{{a:true}},selectedPendingKeys:["a"],supplierRequisitionSelections:[{{type:"order_item",order_item_id:1}}],supplierOrders:[],modal:{{type:"supplierRequisitionDraft"}},
  validateSupplierRequisitionDraft(){{return "";}},draftGroupLines(group){{return group.lines;}},
  loadRequisition:async()=>true,loadSupplierOrders:async()=>true,
  openSupplierOrderPrint(row){{this.modal={{type:"supplierOrderPrint",data:row}};}},
  showToast(message,isError){{toasts.push({{message,isError}});}},errorMessage(error){{return error?.message||"error";}}
}};
vm.saveSupplierRequisitionDraft=new AsyncFunction({json.dumps(block, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.saveSupplierRequisitionDraft();
  const secondPromise=vm.saveSupplierRequisitionDraft();
  await Promise.resolve();
  expect(pending.length===1,"double submit sent more than one POST");
  const second=await secondPromise;
  expect(second?._in_flight===true,"second call was not marked in flight");
  pending[0].resolve({{data:{{created_orders:[{{supplier_name:"鸣朋",supplier_order_id:9}}]}}}});
  const result=await first;
  expect(vm.supplierRequisitionSaveState.committed===true,"successful POST was not marked committed");
  expect(result.created_orders[0].supplier_order_id===9,"successful result was lost");
  const replay=await vm.saveSupplierRequisitionDraft();
  expect(pending.length===1,"committed draft was posted again");
  expect(replay.created_orders[0].supplier_order_id===9,"committed result was not reused");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-save-single-flight.js")


def test_successful_post_survives_refresh_failure_and_network_unknown_closes_draft(
    tmp_path: Path,
) -> None:
    block = _method_body(
        "async saveSupplierRequisitionDraft() {",
        "supplierRequisitionSelectionSignature(selections) {",
    )
    assert "_supplierRequisitionOutcomeUncertain" in INDEX
    assert "结果暂不确定" in INDEX
    assert "不要重复点击原草稿" in INDEX
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let mode="success";let posts=0;
global.axios={{post:async()=>{{posts++;if(mode==="network")throw new Error("连接中断");return {{data:{{created_orders:[{{supplier_name:"嘉林亿",supplier_order_id:7}}]}}}};}}}};
const base=()=>({{
  supplierRequisitionSaveState:{{saving:false,committed:false,uncertain:false,result:null}},
  supplierRequisitionDraft:{{supplier_groups:[{{supplier_name:"嘉林亿",request_key:"abcdef1234567890",lines:[{{report_length_mm:700,report_width_mm:500,cutting_mode:"一开一",requisition_qty:8,source_items:[{{source_type:"order_item",order_item_id:2,source_quantity:8,requisition_qty:8}}]}}]}}]}},
  requisitionSelected:{{a:true}},selectedPendingKeys:["a"],supplierRequisitionSelections:[{{type:"order_item",order_item_id:2}}],supplierOrders:[],modal:{{type:"supplierRequisitionDraft"}},
  validateSupplierRequisitionDraft(){{return "";}},draftGroupLines(group){{return group.lines;}},
  loadRequisition:async()=>{{throw new Error("刷新失败");}},loadSupplierOrders:async()=>false,
  openSupplierOrderPrint(){{throw new Error("刷新失败时不应打开旧打印数据");}},showToast(){{}}
}});
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const vm=base();vm.saveSupplierRequisitionDraft=new AsyncFunction({json.dumps(block, ensure_ascii=False)}).bind(vm);
  const saved=await vm.saveSupplierRequisitionDraft();
  expect(saved._refresh_failed===true,"refresh failure hid the committed result");
  expect(vm.supplierRequisitionSaveState.committed===true&&vm.modal===null,"committed draft remained resubmittable");
  mode="network";const uncertain=base();uncertain.saveSupplierRequisitionDraft=new AsyncFunction({json.dumps(block, ensure_ascii=False)}).bind(uncertain);
  let caught=null;try{{await uncertain.saveSupplierRequisitionDraft();}}catch(error){{caught=error;}}
  expect(caught?._supplierRequisitionOutcomeUncertain===true,"network outcome was not marked uncertain");
  expect(uncertain.supplierRequisitionSaveState.uncertain===true&&uncertain.modal===null,"unknown result left the original draft open");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "requisition-save-outcomes.js")


def test_backend_request_key_idempotency_contract_remains_present() -> None:
    source = (ROOT / "app" / "api" / "requisition.py").read_text(encoding="utf-8")
    model = (ROOT / "app" / "models" / "supplier_requisition_order.py").read_text(
        encoding="utf-8"
    )
    assert "request_key.in_(request_keys)" in source
    assert "idempotent_replay=True" in source
    assert 'detail="该批报料已有部分请求完成，请刷新已报料列表核对，禁止重复生成"' in source
    assert "String(64), nullable=True, unique=True" in model

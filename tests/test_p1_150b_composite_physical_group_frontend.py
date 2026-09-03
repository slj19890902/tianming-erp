from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX and next_signature in INDEX
    return (
        INDEX.split(signature, 1)[1]
        .split(next_signature, 1)[0]
        .rsplit("}", 1)[0]
    )


def test_frozen_physical_group_uses_server_purpose_without_manual_surplus_choice(
    tmp_path: Path,
) -> None:
    required = _method_body(
        "incomingSurplusDispositionRequired(row) {",
        "incomingPurposeProjection(row) {",
    )
    quantity_status = _method_body(
        "incomingReceiptQuantityStatus(row) {",
        "incomingFrozenSurplusQuantity(row) {",
    )
    projection = _method_body(
        "incomingPurposeProjection(row) {",
        "onIncomingQuantityChanged(row) {",
    )
    payload = _method_body(
        "incomingPayload(row) {",
        "canReceiveIncoming(row) {",
    )
    issue = _method_body(
        "incomingReceiptExecutionIssue(row) {",
        "toggleAllIncoming(checked) {",
    )
    can_receive = _method_body(
        "canReceiveIncoming(row) {",
        "incomingSelectedCount() {",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const required=new Function("row",{json.dumps(required, ensure_ascii=False)});
const quantityStatus=new Function("row",{json.dumps(quantity_status, ensure_ascii=False)});
const projection=new Function("row",{json.dumps(projection, ensure_ascii=False)});
const payload=new Function("row",{json.dumps(payload, ensure_ascii=False)});
const issue=new Function("row",{json.dumps(issue, ensure_ascii=False)});
const canReceive=new Function("row",{json.dumps(can_receive, ensure_ascii=False)});
const vm={{
  incomingLocations:[],
  incomingFrozenSurplusQuantity(row){{
    const received=Math.max(Number(row?.incoming_quantity||0),0);
    const remaining=Math.max(Number(row?.remaining_order_purpose_sheet_qty||0),0);
    return Math.max(received-remaining,0);
  }},
  incomingSurplusDispositionRequired(row){{return required.call(vm,row);}},
  incomingPurposeProjection(row){{return projection.call(vm,row);}},
  incomingReceiptExecutionIssue(row){{return issue.call(vm,row);}},
  incomingProjectedVariance(row){{
    return Number(row.cumulative_received_quantity||0)+Number(row.incoming_quantity||0)-Number(row.planned_quantity||0);
  }},
}};
const row={{
  item_id:"cg17",
  expected_group_version:7,
  planned_quantity:3,
  cumulative_received_quantity:2,
  material_status:"pending",
  requisition_status:"已报料",
  purpose_status:"frozen",
  surplus_choice_required:false,
  incoming_quantity:1,
  remaining_order_purpose_sheet_qty:0,
  expected_order_purpose_sheet_qty:0,
  expected_reserve_purpose_sheet_qty:1,
  expected_finished_output_qty:0,
  receipt_execution_ready:true,
  receipt_location_ready:true,
  finished_location_ready:true,
  reserve_location_ready:true,
}};
if(required.call(vm,row)!==false)throw new Error("physical group requested a manual surplus choice");
const quantityState=quantityStatus.call(vm,row);
if(quantityState.label!=="数量相符"||quantityState.warning!==false)throw new Error("planned reserve was mislabeled as order overreceipt");
const projected=projection.call(vm,row);
if(projected.orderSheets!==0||projected.reserveSheets!==1||projected.finishedOutput!==0){{
  throw new Error(`server purpose projection was changed: ${{JSON.stringify(projected)}}`);
}}
if(issue.call(vm,row)!=="")throw new Error("physical group was blocked despite ready server locations");
if(canReceive.call(vm,row)!==true)throw new Error("physical-group reserve receipt stayed disabled");
const body=payload.call(vm,row);
    if(body.surplus_disposition!==null)throw new Error("client invented a physical-group surplus decision");
    if(body.expected_group_version!==7)throw new Error("desktop omitted the physical-group concurrency version");
row.remaining_order_purpose_sheet_qty=2;
row.remaining_reserve_purpose_sheet_qty=1;
row.reserve_sheet_quantity=1;
row.yield_per_sheet=4;
row.incoming_quantity=1;
const firstPartial=projection.call(vm,row);
if(firstPartial.orderSheets!==1||firstPartial.reserveSheets!==0||firstPartial.finishedOutput!==4){{
  throw new Error(`first partial group projection was stale: ${{JSON.stringify(firstPartial)}}`);
}}
row.incoming_quantity=3;
const secondPartial=projection.call(vm,row);
if(secondPartial.orderSheets!==2||secondPartial.reserveSheets!==1||secondPartial.finishedOutput!==8){{
  throw new Error(`split group projection was stale: ${{JSON.stringify(secondPartial)}}`);
}}
if(required.call(vm,row)!==false)throw new Error("physical group requested the ordinary surplus choice");
"""
    target = tmp_path / "p1-150b-physical-group-purpose.js"
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


def test_ordinary_frozen_receipt_reacts_to_edited_quantity(tmp_path: Path) -> None:
    required = _method_body(
        "incomingSurplusDispositionRequired(row) {",
        "incomingPurposeProjection(row) {",
    )
    projection = _method_body(
        "incomingPurposeProjection(row) {",
        "onIncomingQuantityChanged(row) {",
    )
    payload = _method_body(
        "incomingPayload(row) {",
        "canReceiveIncoming(row) {",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const required=new Function("row",{json.dumps(required, ensure_ascii=False)});
const projection=new Function("row",{json.dumps(projection, ensure_ascii=False)});
const payload=new Function("row",{json.dumps(payload, ensure_ascii=False)});
const vm={{
  incomingLocations:[],
  incomingFrozenSurplusQuantity(row){{
    const received=Math.max(Number(row?.incoming_quantity||0),0);
    const remaining=Math.max(Number(row?.remaining_order_purpose_sheet_qty||0),0);
    return Math.max(received-remaining,0);
  }},
  incomingSurplusDispositionRequired(row){{return required.call(vm,row);}},
  incomingPurposeProjection(row){{return projection.call(vm,row);}},
  incomingReceiptExecutionIssue(){{return "";}},
  incomingProjectedVariance(row){{
    return Number(row.cumulative_received_quantity||0)+Number(row.incoming_quantity||0)-Number(row.planned_quantity||0);
  }},
}};
const row={{
  item_id:17,
  purpose_status:"frozen",
  surplus_choice_required:false,
  incoming_quantity:100,
  planned_quantity:100,
  cumulative_received_quantity:0,
  remaining_order_purpose_sheet_qty:100,
  remaining_reserve_purpose_sheet_qty:0,
  expected_order_purpose_sheet_qty:100,
  expected_reserve_purpose_sheet_qty:0,
  expected_finished_output_qty:200,
  finished_disposition_expected_order_purpose_sheet_qty:100,
  finished_disposition_expected_finished_output_qty:200,
  semi_finished_reserve_expected_order_purpose_sheet_qty:100,
  semi_finished_reserve_expected_finished_output_qty:200,
  surplus_disposition:"",
}};
if(required.call(vm,row)!==false)throw new Error("the original 100-sheet receipt requested surplus handling");
row.incoming_quantity=120;
if(required.call(vm,row)!==true)throw new Error("edited 120-sheet receipt bypassed surplus handling");
let rejected=false;
try{{payload.call(vm,row);}}catch(error){{rejected=String(error.message||error).includes("请选择做成品或片料备库");}}
if(!rejected)throw new Error("edited overreceipt was accepted without an explicit choice");
row.surplus_disposition="semi_finished_reserve";
const reserve=projection.call(vm,row);
if(reserve.orderSheets!==100||reserve.reserveSheets!==20||reserve.finishedOutput!==200){{
  throw new Error(`edited reserve projection is wrong: ${{JSON.stringify(reserve)}}`);
}}
row.surplus_disposition="finished";
const finished=projection.call(vm,row);
if(finished.orderSheets!==120||finished.reserveSheets!==0||finished.finishedOutput!==240){{
  throw new Error(`edited finished projection is wrong: ${{JSON.stringify(finished)}}`);
}}
row.incoming_quantity=50;
row.surplus_disposition="";
if(required.call(vm,row)!==false)throw new Error("short receipt incorrectly requested surplus handling");
const shortReceipt=projection.call(vm,row);
if(shortReceipt.orderSheets!==50||shortReceipt.reserveSheets!==0||shortReceipt.finishedOutput!==100){{
  throw new Error(`edited 50-sheet projection stayed on the server default: ${{JSON.stringify(shortReceipt)}}`);
}}
"""
    target = tmp_path / "p1-150b-ordinary-edited-receipt.js"
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


def test_mobile_posts_group_version_and_labels_component_output_truthfully() -> None:
    receive = MOBILE.split("async function receive(itemId) {", 1)[1].split(
        "async function acceptShortNow(itemId) {", 1
    )[0]
    assert "expected_group_version: item.expected_group_version ?? null" in receive
    assert "purpose.order_purpose_sheet_qty" in receive
    assert "purpose.reserve_purpose_sheet_qty" in receive
    assert "purpose.component_output_piece_quantity" in receive
    assert "形成真实组件" in receive

    history = MOBILE.split("const receivedPurposeBlock", 1)[1].split(
        "const primaryAction", 1
    )[0]
    assert "item.composite_physical_purchase_group_id" in history
    assert "purpose_allocation.order_purpose_sheet_qty" in history
    assert "purpose_allocation.reserve_purpose_sheet_qty" in history
    assert "purpose_allocation.component_output_piece_quantity" in history
    assert "形成真实组件" in history
    pending_card = MOBILE.split("const decisionBlock", 1)[1].split(
        "const receiptLocationBlock", 1
    )[0]
    assert "item.composite_physical_purchase_group_id" in pending_card
    assert "预计形成真实组件" in pending_card

    desktop_pending = INDEX.split(
        '<table class="incoming-table incoming-compact-table">', 1
    )[1].split("</data-panel>", 1)[0]
    assert "row.composite_physical_purchase_group_id" in desktop_pending
    assert "预计形成真实组件" in desktop_pending


def test_desktop_receive_success_executes_group_message_without_reference_error(
    tmp_path: Path,
) -> None:
    receive = _method_body(
        "async receiveIncoming(row) {",
        "async acceptShortIncoming(row) {",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.createIdempotencyKey=()=>"p1-150b-ui-success";
global.axios={{put:async()=>({{data:{{
  composite_physical_purchase_group_id:17,
  material_status:"pending",
  remaining_quantity:1,
  purpose_allocation:{{
    order_purpose_sheet_qty:0,
    reserve_purpose_sheet_qty:1,
    component_output_piece_quantity:0,
  }},
}}}})}};
const messages=[];
const guides=[];
const vm={{
  incomingReceiveAttempts:{{}},
  incomingPendingAppliedPage:1,
  authGeneration:1,
  user:{{id:9}},
  incomingReceiptExecutionIssue(){{return "";}},
  async ensureAutomaticPurchaseReceiptFact(){{}},
  incomingPayload(row){{return {{item_id:row.item_id,expected_group_version:7}};}},
  async refreshIncomingAfterWrite(){{return true;}},
  showIncomingNextStepGuide(value){{guides.push(value);}},
  showToast(message,isError=false){{messages.push({{message,isError}});}},
  errorMessage(error){{return String(error?.message||error);}},
}};
const run=new AsyncFunction("row",{json.dumps(receive, ensure_ascii=False)});
(async()=>{{
  await run.call(vm,{{item_id:"cg17",incoming_quantity:1,source_type:"composite_physical_group"}});
  if(messages.length!==1||messages[0].isError)throw new Error(`unexpected toast ${{JSON.stringify(messages)}}`);
  if(!messages[0].message.includes("订单用途 0 张")||!messages[0].message.includes("片料备库 1 张")||!messages[0].message.includes("形成真实组件 0 片")){{
    throw new Error(`group success message lost server facts: ${{messages[0].message}}`);
  }}
  if(guides.length!==0)throw new Error("physical components incorrectly offered direct delivery");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    target = tmp_path / "p1-150b-desktop-receive-success.js"
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


def test_desktop_batch_receive_guides_only_ordinary_order_items(tmp_path: Path) -> None:
    batch_receive = _method_body(
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.createIdempotencyKey=()=>"p1-150b-batch-idempotency";
global.axios={{put:async(_url,payload)=>({{data:{{
  succeeded:payload.items.length,
  failed:0,
  results:payload.items.map(item=>({{
    success:true,
    item_id:item.item_id,
    item:{{material_status:item.item_id===17?"pending":"received"}},
  }})),
}}}})}};
const run=new AsyncFunction({json.dumps(batch_receive, ensure_ascii=False)});
function makeVm(rows){{
  const guides=[];
  const messages=[];
  return {{
    vm:{{
      incomingBatchReceiveAttempt:null,
      incomingPendingAppliedPage:1,
      authGeneration:1,
      user:{{id:9}},
      incomingPending:rows,
      incomingSelected:Object.fromEntries(rows.map(row=>[row.item_id,true])),
      canReceiveIncoming(){{return true;}},
      incomingReceiptExecutionIssue(){{return "";}},
      async ensureAutomaticPurchaseReceiptFact(){{}},
      incomingPayload(row){{return {{item_id:row.item_id}};}},
      async refreshIncomingAfterWrite(){{return true;}},
      showIncomingNextStepGuide(value){{guides.push(value);}},
      showToast(message,isError=false){{messages.push({{message,isError}});}},
      errorMessage(error){{return String(error?.message||error);}},
    }},
    guides,
    messages,
  }};
}}
(async()=>{{
  const ordinary={{
    item_id:17,
    incoming_quantity:1,
    source_type:"supplier_order",
    composite_physical_purchase_group_id:null,
  }};
  const physicalGroup={{
    item_id:"cg9",
    incoming_quantity:1,
    source_type:"composite_physical_group",
    composite_physical_purchase_group_id:9,
  }};
  const mixed=makeVm([ordinary,physicalGroup]);
  await run.call(mixed.vm);
  if(mixed.guides.length!==1)throw new Error(`mixed batch guide count ${{JSON.stringify(mixed.guides)}}`);
  if(mixed.guides[0].count!==1||mixed.guides[0].pendingBalance!==true){{
    throw new Error(`ordinary guide changed ${{JSON.stringify(mixed.guides[0])}}`);
  }}
  const groupOnly=makeVm([physicalGroup]);
  await run.call(groupOnly.vm);
  if(groupOnly.guides.length!==0)throw new Error("physical group offered auto-finished/direct-delivery guide");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    target = tmp_path / "p1-150b-desktop-batch-guide.js"
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


def test_physical_group_source_units_are_visible_without_expanding_supplier_print() -> None:
    desktop_source = INDEX.split(
        '<details v-if="row.composite_physical_purchase_group_id"', 1
    )[1].split("</details>", 1)[0]
    for field in (
        "row.source_order_count",
        "row.source_count",
        "row.source_items",
        "source.order_number",
    ):
        assert field in desktop_source
    assert "净需组件" in desktop_source
    assert "source.order_item_id" not in desktop_source

    mobile_source = MOBILE.split("const physicalGroupSources", 1)[1].split(
        "const drawingButton", 1
    )[0]
    for field in (
        "item.source_items",
        "item.source_count",
        "item.source_order_count",
        "source.order_number",
        "source.net_required_piece_quantity",
    ):
        assert field in mobile_source
    assert "净需组件" in mobile_source
    assert "source.order_item_id" not in mobile_source

    draft_source = INDEX.split(
        'v-for="line in requisitionForm.items"', 1
    )[1].split("</td>", 1)[0]
    for label in ("组级换算", "订单用途统一取整", "当前采购", "备库", "净需组件"):
        assert label in draft_source
    assert "订单用途分配" not in draft_source
    allocation = _method_body(
        "compositeGroupSourceAllocationRows(line) {",
        "async autoCoverCompositeDraftLine(line) {",
    )
    assert "_source_net_required_piece_quantity" in allocation
    assert "_allocated_order_purpose_sheet_quantity" not in allocation

    supplier_print = INDEX.split('id="supplier-order-print-area"', 1)[1].split(
        "</table>", 1
    )[0]
    assert 'v-for="(line,index) in supplierOrderPrintLines(modal.data)"' in supplier_print
    assert 'v-for="source' not in supplier_print
    assert 'v-for="(source' not in supplier_print

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"
INDEX = INDEX_PATH.read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX
    assert next_signature in INDEX
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P0-22 frontend regression"
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


def test_incomplete_batch_attempt_rebuilds_selection_and_never_posts_empty_items(
    tmp_path: Path,
) -> None:
    batch_receive = _method_body(
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const writes=[];const toasts=[];let keySequence=0;
global.confirm=()=>true;
global.createIdempotencyKey=()=>`p0-22-${{++keySequence}}`;
global.axios={{put:async(url,payload)=>{{
  writes.push({{url,payload:JSON.parse(JSON.stringify(payload))}});
  if(!Array.isArray(payload.items)||payload.items.length<1)throw new Error("empty batch reached API");
  return {{data:{{succeeded:1,failed:0,results:[{{item_id:194,success:true,item:{{material_status:"received"}}}}]}}}};
}}}};
const row={{item_id:194,incoming_quantity:12,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",purpose_status:"frozen",receipt_execution_ready:true,product_code:"SAFE"}};
const vm={{
  authGeneration:1,user:{{id:7}},incomingPendingAppliedPage:1,
  incomingPending:[row],incomingSelected:{{194:true}},
  // A browser interruption during preparation used to leave this shell. It has
  // no frozen payload and therefore must not be replayed as items=[].
  incomingBatchReceiveAttempt:{{saving:false,committed:false,preparing:false}},
  canReceiveIncoming(){{return true;}},
  async ensureAutomaticPurchaseReceiptFact(){{}},
  incomingPayload(value){{return {{item_id:value.item_id,received_quantity:Number(value.incoming_quantity)}};}},
  showIncomingNextStepGuide(){{}},async refreshIncomingAfterWrite(){{return true;}},
  showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},
  errorMessage(error){{return error?.message||String(error);}},
}};
vm.batchReceiveIncoming=new AsyncFunction({json.dumps(batch_receive, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  await vm.batchReceiveIncoming();
  expect(writes.length===1,"incomplete attempt did not rebuild the current selection");
  expect(writes[0].url==="/api/incoming/batch-receive","wrong batch endpoint");
  expect(writes[0].payload.items.length===1&&writes[0].payload.items[0].item_id===194,"rebuilt payload lost the selected row");

  writes.length=0;toasts.length=0;
  vm.incomingPending=[];vm.incomingSelected={{}};
  vm.incomingBatchReceiveAttempt={{saving:false,committed:false,request_payload:{{idempotency_key:"broken",items:[]}}}};
  await vm.batchReceiveIncoming();
  expect(writes.length===0,"empty batch payload reached the API");
  expect(toasts.at(-1)?.message.includes("没有可提交的收料明细"),"empty batch did not explain the selection problem in Chinese");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-22-incoming-empty-batch.js")


def test_batch_prepare_classifies_mixed_rows_and_keeps_unknown_replay(
    tmp_path: Path,
) -> None:
    batch_receive = _method_body(
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let keySequence=0;let confirmCalls=0;
global.confirm=()=>{{confirmCalls++;return true;}};
global.createIdempotencyKey=()=>`mixed-${{++keySequence}}`;
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
function row(id, extra={{}}){{return {{item_id:id,incoming_quantity:10,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",purpose_status:"frozen",receipt_execution_ready:true,product_code:String(id).toUpperCase(),...extra}};}}
function context(rows, ensure, responses){{
  const writes=[];const toasts=[];const prepared=[];
  global.axios={{put:async(url,payload)=>{{
    writes.push({{url,payload:JSON.parse(JSON.stringify(payload))}});
    const response=responses.shift();
    if(response instanceof Error)throw response;
    return {{data:response || {{succeeded:payload.items.length,failed:0,results:payload.items.map(item=>({{item_id:item.item_id,success:true,item:{{material_status:"received"}}}}))}}}};
  }}}};
  const vm={{
    authGeneration:1,user:{{id:7}},incomingPendingAppliedPage:1,
    incomingPending:rows,incomingSelected:Object.fromEntries(rows.map(value=>[value.item_id,true])),incomingBatchReceiveAttempt:null,
    canReceiveIncoming(value){{return value.material_status==="pending"&&value.receipt_execution_ready!==false;}},
    incomingReceiptExecutionIssue(value){{return value.receipt_execution_ready===false?String(value.purpose_issue||"当前行不可实收"):"";}},
    async ensureAutomaticPurchaseReceiptFact(value){{prepared.push(value.item_id);return ensure(value);}},
    incomingPayload(value){{return {{item_id:value.item_id,received_quantity:Number(value.incoming_quantity)}};}},
    showIncomingNextStepGuide(){{}},async refreshIncomingAfterWrite(){{return true;}},
    showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},
    errorMessage(error){{return error?.response?.data?.detail?.message||error?.message||String(error);}},
  }};
  vm.batchReceiveIncoming=new AsyncFunction({json.dumps(batch_receive, ensure_ascii=False)}).bind(vm);
  return {{vm,writes,toasts,prepared}};
}}
(async()=>{{
  const serverUncertain=Object.assign(new Error("服务器暂时无响应"),{{response:{{status:500}}}});
  const mixedPrice=context(
    [row("good"),row("price")],
    async value=>{{if(value.item_id==="price")throw Object.assign(new Error("Request failed"),{{response:{{data:{{detail:{{message:"正式采购价格合同不完整"}}}}}}}});}},
    [serverUncertain,null],
  );
  await mixedPrice.vm.batchReceiveIncoming();
  expect(mixedPrice.writes.length===1,"valid row was blocked by another row's price preparation error");
  expect(mixedPrice.writes[0].payload.items.length===1&&mixedPrice.writes[0].payload.items[0].item_id==="good","price-failed row leaked into the batch payload");
  const frozenPayload=JSON.stringify(mixedPrice.writes[0].payload);
  expect(mixedPrice.vm.incomingBatchReceiveAttempt&&!mixedPrice.vm.incomingBatchReceiveAttempt.committed,"unknown result did not preserve the valid frozen payload");
  expect(mixedPrice.toasts.at(-1).message.includes("PRICE")&&mixedPrice.toasts.at(-1).message.includes("正式采购价格合同不完整"),"price preparation failure was not summarized during uncertainty");
  await mixedPrice.vm.batchReceiveIncoming();
  expect(mixedPrice.writes.length===2&&JSON.stringify(mixedPrice.writes[1].payload)===frozenPayload,"unknown retry did not replay the exact valid subset");
  expect(mixedPrice.prepared.join("|")==="good|price","unknown retry reran price preparation");
  expect(mixedPrice.toasts.at(-1).message.includes("成功 1 条")&&mixedPrice.toasts.at(-1).message.includes("未提交 1 条"),"successful replay lost the preparation failure summary");

  const locationIssue="三楼成品位置尚未准备好，请先发布可用库位";
  const mixedInvalid=context(
    [row("good2"),row("blocked",{{receipt_execution_ready:false,purpose_issue:locationIssue}})],
    async()=>{{}},
    [null],
  );
  await mixedInvalid.vm.batchReceiveIncoming();
  expect(mixedInvalid.writes.length===1&&mixedInvalid.writes[0].payload.items.length===1&&mixedInvalid.writes[0].payload.items[0].item_id==="good2","invalid selected row blocked or polluted the valid subset");
  expect(mixedInvalid.prepared.join("|")==="good2","invalid row called price preparation");
  expect(mixedInvalid.toasts.at(-1).message.includes("BLOCKED")&&mixedInvalid.toasts.at(-1).message.includes(locationIssue),"invalid selected row was silently discarded");

  const allFailed=context(
    [row("blocked2",{{receipt_execution_ready:false,purpose_issue:locationIssue}}),row("price2")],
    async value=>{{if(value.item_id==="price2")throw new Error("采购价格事实冻结失败");}},
    [],
  );
  await allFailed.vm.batchReceiveIncoming();
  expect(allFailed.writes.length===0,"all-failed selection posted an empty or partial request");
  expect(allFailed.vm.incomingBatchReceiveAttempt===null,"all-failed preparation left a replay shell");
  expect(allFailed.toasts.at(-1).message.includes("成功 0 条")&&allFailed.toasts.at(-1).message.includes("未提交 2 条"),"all-failed result did not report exact counts");
  expect(allFailed.toasts.at(-1).message.includes("BLOCKED2")&&allFailed.toasts.at(-1).message.includes("PRICE2"),"all-failed result omitted a row reason");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-22-incoming-mixed-prepare.js")


def test_receipt_execution_blocker_prevents_price_and_receive_calls(tmp_path: Path) -> None:
    receive = _method_body(
        "async receiveIncoming(row) {",
        "async acceptShortIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const writes=[];const toasts=[];let factCalls=0;
global.createIdempotencyKey=()=>"unused";
global.axios={{put:async(url,payload)=>{{writes.push({{url,payload}});throw new Error("write must be blocked");}}}};
const row={{item_id:194,incoming_quantity:12,material_status:"pending",requisition_status:"已报料",purpose_status:"frozen",receipt_fact_ready:false,receipt_execution_ready:false,finished_location_ready:false,purpose_issue:"三楼成品位置尚未准备好，请先发布可用库位"}};
const vm={{
  incomingReceiveAttempts:{{}},incomingPendingAppliedPage:1,authGeneration:1,user:{{id:7}},
  incomingReceiptExecutionIssue(value){{return value.receipt_execution_ready===false?value.purpose_issue:"";}},
  async ensureAutomaticPurchaseReceiptFact(){{factCalls++;}},
  incomingPayload(){{throw new Error("payload must not be built");}},
  showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},
  errorMessage(error){{return error?.message||String(error);}},
}};
vm.receiveIncoming=new AsyncFunction("row",{json.dumps(receive, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  await vm.receiveIncoming(row);
  expect(factCalls===0,"location-blocked row incorrectly called the price-fact endpoint");
  expect(writes.length===0,"location-blocked row reached the receive endpoint");
  expect(toasts.at(-1)?.message.includes("三楼成品位置尚未准备好"),"server purpose issue was not shown");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-22-incoming-location-block.js")


def test_auto_freeze_uses_only_price_fact_readiness(tmp_path: Path) -> None:
    automatic = _method_body(
        "async ensureAutomaticPurchaseReceiptFact(row) {",
        "incomingPayload(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const writes=[];
global.createIdempotencyKey=()=>"auto-fact-key";
global.axios={{put:async(url,payload)=>{{writes.push({{url,payload}});return {{data:{{receipt_fact_version:8,actual_material_version:3,actual_material_fingerprint:"fact-3",actual_material_id:16}}}};}}}};
const vm={{allMaterials:[{{id:16,code:"K3",is_active:true}}]}};
vm.ensureAutomaticPurchaseReceiptFact=new AsyncFunction("row",{json.dumps(automatic, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const frozen={{purpose_status:"frozen",receipt_fact_ready:true,receipt_location_ready:false,source_key:"supplier_order:8",material_code:"K3"}};
  await vm.ensureAutomaticPurchaseReceiptFact(frozen);
  expect(writes.length===0,"an existing frozen price fact was recreated because a location was blocked");

  const missing={{purpose_status:"frozen",receipt_fact_ready:false,receipt_location_ready:true,source_key:"supplier_order:9",material_code:"K3",purchase_purpose_source_snapshot_id:4,expected_purpose_snapshot_version:2,receipt_plan_fingerprint:"plan",expected_source_version:5,latest_receipt_fact_version:0}};
  await vm.ensureAutomaticPurchaseReceiptFact(missing);
  expect(writes.length===1&&writes[0].url.endsWith("/receipt-facts/auto"),"a truly missing price fact was not auto-frozen");
  expect(missing.receipt_fact_ready===true&&missing.expected_receipt_fact_version===8,"auto-freeze response was not applied to the row");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-22-incoming-auto-price-fact.js")


def test_pydantic_empty_items_error_is_precisely_localized(tmp_path: Path) -> None:
    error_message = _method_body("errorMessage(error) {", "normalizeValidationErrors(")
    script = f"""
const FunctionCtor=Function;
const vm={{}};
vm.errorMessage=new FunctionCtor("error",{json.dumps(error_message, ensure_ascii=False)}).bind(vm);
const message=vm.errorMessage({{response:{{data:{{detail:[{{type:"too_short",loc:["body","items"],msg:"List should have at least 1 item after validation, not 0",ctx:{{field_type:"List",min_length:1,actual_length:0}}}}]}}}}}});
if(message!=="本次没有可提交的收料明细，请重新勾选后再试")throw new Error(`unexpected localization: ${{message}}`);
if(message.includes("List should")||message.includes("validation"))throw new Error("Pydantic English leaked to the operator");
"""
    _run_node(script, tmp_path, "p0-22-incoming-validation-localization.js")


def test_template_exposes_exact_receipt_execution_blocker() -> None:
    table = INDEX.split('<table class="incoming-table incoming-compact-table">', 1)[1].split(
        "</data-panel>", 1
    )[0]
    assert "incomingReceiptExecutionIssue(row)" in table
    assert "暂不能实收" in table
    assert "purpose_issue" in INDEX
    assert "receipt_execution_ready" in INDEX
    assert "receipt_location_ready" in INDEX
    assert "finished_location_ready" in INDEX


def test_mobile_location_blocker_precedes_price_fact_and_receive_writes(
    tmp_path: Path,
) -> None:
    receive = MOBILE.split("async function receive(itemId) {", 1)[1].split(
        "async function acceptShortNow(itemId) {", 1
    )[0].rsplit("}", 1)[0]
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const item={{item_id:194,incoming_quantity:12,planned_quantity:12,cumulative_received_quantity:0,purpose_status:"frozen",receipt_fact_ready:false,receipt_execution_ready:false,finished_location_ready:false,purpose_issue:"三楼成品位置尚未准备好，请先发布可用库位"}};
const toasts=[];let factCalls=0;const writes=[];
global.state={{busyItemIds:new Set(),receiveIdempotencyKeys:new Map(),locations:[]}};
global.findItem=()=>item;
global.receiptExecutionIssue=value=>value.receipt_execution_ready===false?value.purpose_issue:"";
global.showToast=(message,isError)=>toasts.push({{message:String(message),isError:!!isError}});
global.toChineseMessage=error=>error?.message||String(error);
global.ensureAutomaticReceiptFact=async()=>{{factCalls++;}};
global.api=async(url,options)=>{{writes.push({{url,options}});throw new Error("write must be blocked");}};
global.createIdempotencyKey=()=>"unused";
global.render=()=>{{}};
global.refreshAfterIncomingWrite=async()=>true;
const receive=new AsyncFunction("itemId",{json.dumps(receive, ensure_ascii=False)});
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  await receive(194);
  expect(factCalls===0,"mobile location blocker called /receipt-facts/auto");
  expect(writes.length===0,"mobile location blocker reached formal receive");
  expect(toasts.at(-1)?.message.includes("三楼成品位置尚未准备好"),"mobile did not show the server purpose issue");
  expect(state.busyItemIds.size===0&&state.receiveIdempotencyKeys.size===0,"blocked mobile receive allocated mutation state");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-22-mobile-location-block.js")


def test_mobile_card_disables_receive_and_renders_purpose_issue() -> None:
    render = MOBILE.split("function renderCard(item) {", 1)[1].split(
        "function applyIncomingPrimarySpecLayout", 1
    )[0]
    assert "receiptExecutionIssue(item)" in render
    assert "暂不能实收" in render
    assert "purpose_issue" in MOBILE
    for field in (
        "receipt_execution_ready",
        "receipt_location_ready",
        "finished_location_ready",
    ):
        assert field in MOBILE

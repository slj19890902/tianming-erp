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
    assert node is not None, "Node.js is required for replenishment save regression"
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


def test_replenishment_form_and_button_have_dedicated_operation_state() -> None:
    assert (
        "stockReplenishmentSaveState:{saving:false, committed:false, uncertain:false, result:null}"
        in INDEX
    )
    open_block = _method_body(
        "async openStockReplenishment(options={}) {",
        "defaultStockLocation(type) {",
    )
    assert "idempotency_key:createIdempotencyKey()" in open_block
    assert "stockReplenishmentSaveState = {saving:false, committed:false, uncertain:false, result:null}" in open_block

    click = INDEX.index("modal?.type==='delivery' ? deliveryPrimaryAction() : saveModal()")
    start = INDEX.rfind("<button", 0, click)
    end = INDEX.index("</button>", click) + len("</button>")
    button = INDEX[start:end]
    assert "stockReplenishmentSaveState.saving" in button
    assert "stockReplenishmentSaveState.committed" in button
    assert "正在保存补库草稿…" in button
    assert "补库草稿已保存" in button


def test_replenishment_save_freezes_payload_and_commits_before_auxiliary_work() -> None:
    block = _method_body(
        "async saveStockReplenishmentDraft() {",
        "async saveCompositeRequisitionDraft() {",
    )
    assert "if (state.saving || state.committed)" in block
    assert "const frozenPayload = JSON.parse(JSON.stringify(payload))" in block
    assert 'axios.post("/api/requisition/stock-replenishment/orders", frozenPayload)' in block
    assert block.index("state.committed = true") < block.index("axios.get(")
    assert block.index("state.committed = true") < block.index("Promise.allSettled")
    assert "_refresh_failed:refreshFailed" in block
    assert "_print_failed:printFailed" in block


def _runtime(block: str) -> str:
    return f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const pending=[];const toasts=[];const prints=[];
global.axios={{
  post:(url,payload)=>new Promise((resolve,reject)=>pending.push({{url,payload,resolve,reject}})),
  get:async url=>({{data:{{id:41,order_number:"CBR-41",items:[]}}}}),
}};
const vm={{
  stockReplenishmentSaveState:{{saving:false,committed:false,uncertain:false,result:null}},
  stockReplenishmentForm:{{source_type:"customer_request",idempotency_key:"manual-key-1",supplier_name:"鸣朋",customer_id:5,remark:"补库",stock_now:false,items:[{{_key:"row-1",target_inventory_type:"semi_finished",product_id:1,customer_id:5,product_code:"22000008",product_name:"外箱",material_id:1,material_code:"A416D",layer_count:5,flute_type:"AB",report_length_mm:800,report_width_mm:600,quantity:20,location_id:2}}]}},
  modal:{{type:"stockReplenishment"}},
  validateStockReplenishmentForm(){{return "";}},
  loadRequisition:async()=>true,
  openStockReplenishmentPrint(data){{prints.push(data);this.modal={{type:"stockReplenishmentPrint"}};}},
  showToast(message,isError){{toasts.push({{message,isError}});}},
}};
vm.saveStockReplenishmentDraft=new AsyncFunction({json.dumps(block, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
"""


def test_same_replenishment_draft_is_single_flight_and_payload_is_frozen(
    tmp_path: Path,
) -> None:
    block = _method_body(
        "async saveStockReplenishmentDraft() {",
        "async saveCompositeRequisitionDraft() {",
    )
    script = _runtime(block) + """
(async()=>{
  const first=vm.saveStockReplenishmentDraft();
  const secondPromise=vm.saveStockReplenishmentDraft();
  await Promise.resolve();
  expect(pending.length===1,"double submit sent more than one POST");
  vm.stockReplenishmentForm.supplier_name="嘉林亿";
  vm.stockReplenishmentForm.items[0].quantity=99;
  expect(pending[0].payload.supplier_name==="鸣朋"&&pending[0].payload.items[0].quantity===20,"payload changed after submit");
  const second=await secondPromise;
  expect(second?._in_flight===true,"second call was not marked in flight");
  pending[0].resolve({data:{id:41,order_number:"CBR-41"}});
  const result=await first;
  expect(result.id===41&&vm.stockReplenishmentSaveState.committed,"successful draft was not committed");
  const replay=await vm.saveStockReplenishmentDraft();
  expect(pending.length===1&&replay.id===41,"committed draft was posted again");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "stock-replenishment-single-flight.js")


def test_successful_post_survives_print_and_refresh_failures(tmp_path: Path) -> None:
    block = _method_body(
        "async saveStockReplenishmentDraft() {",
        "async saveCompositeRequisitionDraft() {",
    )
    script = _runtime(block) + """
(async()=>{
  axios.get=async()=>{throw new Error("打印读取失败");};
  vm.loadRequisition=async()=>{throw new Error("列表刷新失败");};
  const request=vm.saveStockReplenishmentDraft();
  pending[0].resolve({data:{id:42,order_number:"CBR-42"}});
  const result=await request;
  expect(result._print_failed===true&&result._refresh_failed===true,"auxiliary failures were hidden");
  expect(vm.stockReplenishmentSaveState.committed===true&&vm.modal===null,"successful draft remained resubmittable");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "stock-replenishment-success-boundary.js")


def test_unknown_network_result_closes_original_draft_and_has_clear_message() -> None:
    assert "_stockReplenishmentOutcomeUncertain" in INDEX
    assert "补库草稿提交连接中断，结果暂不确定" in INDEX
    assert "刷新“已报料”和“来料待入库”核对" in INDEX
    assert "不要重复点击原草稿" in INDEX


def test_validation_failure_keeps_draft_but_unknown_result_closes_it(
    tmp_path: Path,
) -> None:
    block = _method_body(
        "async saveStockReplenishmentDraft() {",
        "async saveCompositeRequisitionDraft() {",
    )
    script = _runtime(block) + """
(async()=>{
  const validationRequest=vm.saveStockReplenishmentDraft();
  const validationError=new Error("数量无效");
  validationError.response={status:400,data:{detail:"数量无效"}};
  pending[0].reject(validationError);
  let caught=null;try{await validationRequest;}catch(error){caught=error;}
  expect(caught===validationError,"validation error was replaced");
  expect(vm.stockReplenishmentSaveState.uncertain===false,"known 4xx was marked uncertain");
  expect(vm.modal?.type==="stockReplenishment","known 4xx closed the editable draft");

  const other={...vm,stockReplenishmentSaveState:{saving:false,committed:false,uncertain:false,result:null},modal:{type:"stockReplenishment"}};
  other.saveStockReplenishmentDraft=new AsyncFunction(""" + json.dumps(
        block, ensure_ascii=False
    ) + """).bind(other);
  const networkRequest=other.saveStockReplenishmentDraft();
  const networkError=new Error(\"连接中断\");
  pending[1].reject(networkError);
  let networkCaught=null;try{await networkRequest;}catch(error){networkCaught=error;}
  expect(networkCaught?._stockReplenishmentOutcomeUncertain===true,\"network result was not marked unknown\");
  expect(other.stockReplenishmentSaveState.uncertain===true&&other.modal===null,\"unknown result left original draft open\");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "stock-replenishment-error-boundary.js")


def test_backend_has_manual_idempotency_and_unique_conflict_replay_contract() -> None:
    source = (ROOT / "app" / "api" / "requisition.py").read_text(encoding="utf-8")
    block = source.split("def create_stock_replenishment_order(", 1)[1].split(
        '@router.get("/stock-replenishment/orders")', 1
    )[0]
    assert '"CBR" if payload.source_type == "customer_request" else "CBW"' in block
    assert "if payload.idempotency_key:" in block
    assert "except IntegrityError:" in block
    assert "idempotent_order_number" in block
    assert "return replenishment_order_dict(existing_order)" in block

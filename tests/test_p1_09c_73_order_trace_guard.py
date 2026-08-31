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
    assert node is not None, "Node.js is required for order trace regression tests"
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


def _runtime(methods: dict[str, tuple[list[str], str]]) -> str:
    compiled_parts = []
    for name, (params, body) in methods.items():
        arguments = [json.dumps(param) for param in params]
        arguments.append(json.dumps(body, ensure_ascii=False))
        compiled_parts.append(f"{name}:new AsyncFunction({','.join(arguments)})")
    compiled = ",\n".join(compiled_parts)
    return f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();global.window={{scrollY:88}};global.document={{getElementById:()=>null}};
const pending=[];const toasts=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const methods={{{compiled}}};
const vm={{
  modal:null,orderDetail:null,orderGroupDetail:null,orderTrace:null,orderTraceEventDetail:null,
  orderTraceReturnContext:null,orderTraceEventDetailLoadingKey:"",filters:{{orderScope:"active"}},
  pages:{{orders:3}},expandedOrders:{{groupA:true}},
  beginLatestRequest(key){{global.latestRequestControllers.get(key)?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="CanceledError";}},
  cancelOrderTraceEventDetailRequest(){{global.latestRequestControllers.get("orders:trace-event-detail")?.abort();global.latestRequestControllers.delete("orders:trace-event-detail");this.orderTraceEventDetailLoadingKey="";}},
  cancelOrderTraceChildRequests(){{for(const key of ["orders:trace-reload","orders:trace-event-detail"]){{global.latestRequestControllers.get(key)?.abort();global.latestRequestControllers.delete(key);}}}},
  rememberModalOpener(){{return null;}},focusAccessibleModal(){{}},
  showToast(message,isError){{toasts.push({{message,isError}});}},errorMessage(error){{return error?.message||String(error);}},displayOrderNumber(row){{return row.order_number||row.id;}},
  $nextTick(callback){{if(callback)callback();}}
}};
for(const [name,method] of Object.entries(methods))vm[name]=method.bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
"""


def test_top_level_order_read_modals_share_latest_request_guard() -> None:
    ensure = _method_body(
        "async ensureOrderGroupDetail(group, {force=false}={}) {",
        "async toggleOrderGroup(group) {",
    )
    group = _method_body("async openOrderGroupDetail(group) {", "async openCostGaps() {")
    detail = _method_body("async openOrderDetail(row, {keepModalA11ySession=false, returnFocusFallback=null}={}) {", "openEstimatedCost(order, item) {")
    trace = _method_body("async openOrderTrace(order, item) {", "traceCurrentEvent() {")

    assert "orderGroupDetailRequests.get(key)" in ensure
    assert "signal:controller.signal" in ensure
    for body in (group, detail, trace):
        assert 'const requestKey = "orders:read-detail-modal";' in body
        assert "beginLatestRequest(requestKey)" in body
        assert "latestRequestControllers.get(requestKey) !== controller" in body
        assert "finishLatestRequest(requestKey, controller)" in body
    assert "signal:controller.signal" in detail
    assert "signal:controller.signal" in trace
    assert "await this.ensureOrderGroupDetail(group)" in group


def test_latest_trace_beats_older_order_detail_and_owns_return_context(tmp_path: Path) -> None:
    detail = _method_body("async openOrderDetail(row, {keepModalA11ySession=false, returnFocusFallback=null}={}) {", "openEstimatedCost(order, item) {")
    trace = _method_body("async openOrderTrace(order, item) {", "traceCurrentEvent() {")
    script = _runtime({
        "openOrderDetail": (["row", "{keepModalA11ySession=false, returnFocusFallback=null}={}"], detail),
        "openOrderTrace": (["order", "item"], trace),
    }) + """
(async()=>{
  const first=vm.openOrderDetail({id:1,order_number:"A"});
  const second=vm.openOrderTrace({id:2,order_number:"B"},{id:22,item_sequence:2});
  pending[1].resolve({data:{order:{id:2},item:{id:22,item_sequence:2},events:[],current_inventory:[],restricted_stages:[]}});
  expect(await second===true,"latest trace did not open");
  pending[0].resolve({data:{id:1,order_number:"A"}});
  expect(await first===false,"stale detail response was accepted");
  expect(vm.modal?.type==="orderTrace"&&vm.orderTrace?.order?.id===2,"stale detail replaced the current trace");
  expect(vm.orderTraceReturnContext?.orderId===2&&vm.orderTraceReturnContext?.itemId===22,"return context was not owned by latest trace");
  expect(toasts.length===0,"stale request leaked an error");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "order-trace-latest.js")


def test_trace_event_detail_latest_response_wins_and_stale_error_is_silent(tmp_path: Path) -> None:
    event_detail = _method_body("async openTraceEventDetail(event) {", "returnFromOrderTrace() {")
    script = _runtime({"openTraceEventDetail": (["event"], event_detail)}) + """
(async()=>{
  vm.modal={type:"orderTrace"};vm.orderTrace={order:{id:7},item:{id:8},events:[]};
  const first=vm.openTraceEventDetail({key:"A",source_type:"incoming",source_id:10});
  const second=vm.openTraceEventDetail({key:"B",source_type:"delivery",source_id:20});
  pending[1].resolve({data:{navigation:{order_id:7,item_id:8,source_type:"delivery",source_id:20},event:{}}});
  expect(await second===true,"latest event detail did not complete");
  const staleError=new Error("旧阶段失败");staleError.name="CanceledError";pending[0].reject(staleError);
  expect(await first===false,"stale event error was accepted");
  expect(vm.orderTraceEventDetail?.navigation?.source_id===20,"stale event replaced latest detail");
  expect(toasts.length===0&&vm.orderTraceEventDetailLoadingKey==="","stale event leaked feedback or loading state");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "order-trace-event-latest.js")


def test_trace_reload_cannot_write_into_switched_trace(tmp_path: Path) -> None:
    reload_body = _method_body("async reloadOrderTrace() {", "async rollbackTraceEvent(event) {")
    script = _runtime({"reloadOrderTrace": ([], reload_body)}) + """
(async()=>{
  const traceA={order:{id:1},item:{id:11},events:[]};
  const traceB={order:{id:2},item:{id:22},events:[]};
  vm.modal={type:"orderTrace"};vm.orderTrace=traceA;
  const request=vm.reloadOrderTrace();
  vm.orderTrace=traceB;
  pending[0].resolve({data:{order:{id:1},item:{id:11},events:[{key:"old"}]}});
  expect(await request===false,"stale reload was accepted");
  expect(vm.orderTrace===traceB,"stale reload overwrote the switched trace");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(script, tmp_path, "order-trace-reload.js")


def test_closing_order_read_modals_cancels_all_related_requests() -> None:
    cancel_start = INDEX.index("cancelOrderTraceEventDetailRequest() {")
    cancel_end = INDEX.index("async openOrderGroupDetail(group) {", cancel_start)
    cancel = INDEX[cancel_start:cancel_end]
    returned = _method_body("returnFromOrderTrace() {", "traceEventTime(event) {")
    close = _method_body("closeModal() {", "handleMasterSaveRefreshFailure(")

    for key in (
        "orders:read-detail-modal",
        "orders:trace-reload",
        "orders:trace-event-detail",
    ):
        assert key in cancel
    assert "this.cancelOrderReadDetailRequests()" in returned
    assert "orderGroupDetail" in close and "orderDetail" in close
    assert "this.cancelOrderReadDetailRequests()" in close


def test_order_detail_and_trace_reads_remain_read_only() -> None:
    block = "\n".join(
        (
            _method_body(
                "async ensureOrderGroupDetail(group, {force=false}={}) {",
                "async toggleOrderGroup(group) {",
            ),
            _method_body("async openOrderGroupDetail(group) {", "async openCostGaps() {"),
            _method_body("async openOrderDetail(row, {keepModalA11ySession=false, returnFocusFallback=null}={}) {", "openEstimatedCost(order, item) {"),
            _method_body("async openOrderTrace(order, item) {", "traceCurrentEvent() {"),
            _method_body("async reloadOrderTrace() {", "async rollbackTraceEvent(event) {"),
            _method_body("async openTraceEventDetail(event) {", "returnFromOrderTrace() {"),
        )
    )

    assert "axios.get" in block
    assert "axios.post" not in block
    assert "axios.put" not in block
    assert "axios.patch" not in block
    assert "axios.delete" not in block

from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
VERSION = (ROOT / "app" / "version.py").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def test_login_fields_have_programmatic_labels_and_shared_error_description() -> None:
    login = _between(INDEX, '<form class="login-card"', "</form>")
    assert 'for="erp-login-username"' in login
    assert 'id="erp-login-username"' in login
    assert 'for="erp-login-password"' in login
    assert 'id="erp-login-password"' in login
    assert 'id="erp-login-remember"' in login
    assert 'for="erp-login-remember"' in login
    assert 'id="erp-login-error"' in login
    assert 'role="alert"' in login
    assert 'aria-live="assertive"' in login
    assert ':aria-describedby="loginError ? \'erp-login-error\' : null"' in login
    assert ':aria-invalid="!!loginError"' in login


def test_order_detail_uses_a_complete_dialog_contract() -> None:
    modal = _between(INDEX, '<div v-if="modal" class="modal-mask"', "<!-- 共用图纸预览")
    assert ':role="isAccessibleBusinessDialog ? \'dialog\' : null"' in modal
    assert ':aria-modal="isAccessibleBusinessDialog ? \'true\' : null"' in modal
    assert ':aria-labelledby="isAccessibleBusinessDialog ? \'erp-business-dialog-title\' : null"' in modal
    assert ':aria-describedby="isAccessibleReadOnlyOrderDialog ? \'erp-order-detail-description\' : null"' in modal
    assert ':tabindex="isAccessibleBusinessDialog ? -1 : null"' in modal
    assert ':id="isAccessibleBusinessDialog ? \'erp-business-dialog-title\' : null"' in modal
    assert 'id="erp-order-detail-description"' in modal
    assert '@keydown="onModalKeydown"' in modal
    assert 'ref="businessModal"' in modal
    assert 'rememberModalOpener' in INDEX
    assert 'focusAccessibleModal' in INDEX
    assert 'focusableElements' in INDEX
    assert 'event.key !== "Tab"' in INDEX
    assert 'document.body.classList.add("business-modal-open")' in INDEX
    assert 'document.body.classList.remove("business-modal-open")' in INDEX
    assert 'restoreModalOpenerFocus' in INDEX


def test_order_detail_child_modal_session_keeps_lock_and_original_opener() -> None:
    detail = _between(
        INDEX,
        "async openOrderDetail(row, {keepModalA11ySession=false, returnFocusFallback=null}={}) {",
        "openEstimatedCost(order, item) {",
    )
    estimated = _between(INDEX, "openEstimatedCost(order, item) {", "async saveEstimatedCost() {")
    save = _between(INDEX, "async saveEstimatedCost() {", "async openOrderTrace(order, item) {")
    external = _between(INDEX, "async openExternalPurchase(order) {", "async confirmExternalPurchase() {")
    close = _between(INDEX, "closeModal() {", "handleMasterSaveRefreshFailure(")
    assert "modalA11ySessionActive" in INDEX
    assert "if (modalA11ySessionActive) return modalOpenerElement" in INDEX
    assert "const keepCurrentModalSession = !!keepModalA11ySession && modalA11ySessionActive" in detail
    assert "keepCurrentModalSession && modalA11ySessionActive && modalA11ySessionGeneration === expectedModalSessionGeneration" in detail
    assert "if (modalA11ySessionActive) this.focusAccessibleModal()" in estimated
    assert "if (modalA11ySessionActive) this.focusAccessibleModal()" in external
    assert "{keepModalA11ySession:true}" in save
    assert "const expectedModalSessionGeneration = modalA11ySessionActive ? modalA11ySessionGeneration : null" in save
    assert "modalA11ySessionGeneration !== expectedModalSessionGeneration" in save
    assert "原订单详情已关闭，请手动刷新核对" in save
    assert "const restoreReadOnlyOrderFocus = modalA11ySessionActive" in close
    assert "if (restoreReadOnlyOrderFocus) this.restoreModalOpenerFocus()" in close
    drawing = _between(INDEX, '<div v-if="showDrawingPreview"', '<div v-if="pdfWarehouseLocator.visible"')
    assert 'role="dialog"' in drawing and 'aria-modal="true"' in drawing
    assert 'aria-labelledby="erp-drawing-preview-title"' in drawing
    assert '@keydown="onDrawingPreviewKeydown"' in drawing
    assert "closeDrawingPreview" in drawing
    assert 'if (event.key === "Escape")' in INDEX
    assert "if (modalA11ySessionActive) this.$nextTick(() => this.focusAccessibleModal())" in INDEX
    auth_loss = _between(INDEX, "window.erpAuthRequired = () => {", "window.erpForbidden")
    logout = _between(INDEX, "async logout() {", "resetPagePerformanceState() {")
    assert "this.resetModalA11ySession({discardModal:true})" in auth_loss
    assert "this.resetModalA11ySession({discardModal:true})" in logout


def test_auth_loss_discards_active_order_dialog_and_overlay_without_restoring_opener(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    reset = _between(
        INDEX,
        "resetModalA11ySession({discardModal=false}={}) {",
        "onModalKeydown(event) {",
    ).split(") {", 1)[1].rsplit("}", 1)[0]
    harness_reset = reset.replace("modalA11ySessionActive", "globalThis.modalA11ySessionActive").replace(
        "modalA11ySessionGeneration", "globalThis.modalA11ySessionGeneration"
    ).replace("modalOpenerFallbackElement", "globalThis.modalOpenerFallbackElement").replace(
        "modalOpenerElement", "globalThis.modalOpenerElement"
    )
    target = tmp_path / "candidate-d-auth-modal-reset.js"
    target.write_text(
        f'''const Reset=Object.getPrototypeOf(function(){{}}).constructor;
let classRemoved=0;global.document={{body:{{classList:{{remove(value){{if(value!=="business-modal-open")throw new Error("wrong class");classRemoved+=1;}}}}}}}};
let cancelled=0;let restored=0;globalThis.modalA11ySessionActive=true;globalThis.modalA11ySessionGeneration=7;globalThis.modalOpenerElement={{focus(){{restored+=1;}}}};globalThis.modalOpenerFallbackElement={{focus(){{restored+=1;}}}};
const vm={{modal:{{type:"orderDetail"}},orderDetail:{{id:9}},orderGroupDetail:{{id:8}},estimatedCostForm:{{item_id:7}},externalPurchase:{{items:[1]}},showDrawingPreview:true,drawingPreviewUrl:"/private.png",cancelOrderReadDetailRequests(){{cancelled+=1;}}}};
const reset=new Reset("discardModal",{harness_reset!r});
reset.call(vm,true);
if(classRemoved!==1||cancelled!==1)throw new Error("discard hooks");
if(vm.modal!==null||vm.orderDetail!==null||vm.orderGroupDetail!==null||vm.showDrawingPreview!==false||vm.drawingPreviewUrl!=="")throw new Error("old dialog state survived");
if(Object.keys(vm.estimatedCostForm).length||vm.externalPurchase.items.length)throw new Error("child state survived");
if(globalThis.modalA11ySessionActive||globalThis.modalA11ySessionGeneration!==8||globalThis.modalOpenerElement!==null||globalThis.modalOpenerFallbackElement!==null)throw new Error("session state survived");
if(restored!==0)throw new Error("auth loss restored stale opener");''',
        encoding="utf-8",
    )
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def test_focus_trap_wraps_both_directions_and_detached_opener_uses_stable_fallback(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    trap = _between(INDEX, "onModalKeydown(event) {", "async setUiMode(mode) {").split(") {", 1)[1].rsplit("}", 1)[0]
    restore = _between(INDEX, "restoreModalOpenerFocus() {", "resetModalA11ySession(").split("() {", 1)[1].rsplit("}", 1)[0]
    harness_restore = restore.replace("modalA11ySessionActive", "globalThis.modalA11ySessionActive").replace(
        "modalA11ySessionGeneration", "globalThis.modalA11ySessionGeneration"
    ).replace("modalOpenerFallbackElement", "globalThis.modalOpenerFallbackElement").replace(
        "modalOpenerElement", "globalThis.modalOpenerElement"
    )
    target = tmp_path / "candidate-d-focus-contract.js"
    target.write_text(
        f'''const Fn=Object.getPrototypeOf(function(){{}}).constructor;
let prevented=0;let firstFocus=0;let lastFocus=0;const first={{focus(){{firstFocus+=1;}}}};const last={{focus(){{lastFocus+=1;}}}};const container={{focus(){{}},querySelectorAll(){{return [first,last];}}}};
global.document={{activeElement:last,body:{{classList:{{remove(){{}}}}}}}};const vm={{isAccessibleBusinessDialog:true,$refs:{{businessModal:container}},focusableElements(){{return [first,last];}},$nextTick(fn){{fn();}}}};
const trap=new Fn("event",{trap!r});trap.call(vm,{{key:"Tab",shiftKey:false,preventDefault(){{prevented+=1;}}}});if(firstFocus!==1||lastFocus!==0||prevented!==1)throw new Error("forward wrap");
document.activeElement=first;trap.call(vm,{{key:"Tab",shiftKey:true,preventDefault(){{prevented+=1;}}}});if(lastFocus!==1||prevented!==2)throw new Error("reverse wrap");
let primaryFocus=0;let fallbackFocus=0;globalThis.modalA11ySessionActive=true;globalThis.modalA11ySessionGeneration=3;globalThis.modalOpenerElement={{isConnected:false,focus(){{primaryFocus+=1;}}}};globalThis.modalOpenerFallbackElement={{isConnected:true,focus(){{fallbackFocus+=1;}}}};
const restore=new Fn({harness_restore!r});restore.call(vm);if(primaryFocus!==0||fallbackFocus!==1)throw new Error("logical fallback");if(globalThis.modalOpenerElement!==null||globalThis.modalOpenerFallbackElement!==null)throw new Error("opener not cleared");''',
        encoding="utf-8",
    )
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    assert 'ref="costGapTrigger"' in INDEX
    cost_jump = _between(INDEX, "openCostGapOrder(row) {", "async openOrderDetail(")
    assert "returnFocusFallback:this.$refs.costGapTrigger || null" in cost_jump


def test_close_during_estimated_cost_save_cannot_reopen_modal_session(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    save = _between(INDEX, "async saveEstimatedCost() {", "async openOrderTrace(order, item) {").split("{", 1)[1].rsplit("}", 1)[0]
    target = tmp_path / "candidate-d-cost-session.js"
    harness_save = save.replace("modalA11ySessionActive", "globalThis.modalA11ySessionActive").replace(
        "modalA11ySessionGeneration", "globalThis.modalA11ySessionGeneration"
    )
    target.write_text(
        f'''const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let resolvePost;global.axios={{post(){{return new Promise(resolve=>resolvePost=resolve);}}}};
const messages=[];const opens=[];
const vm={{estimatedCostForm:{{item_id:7,order_id:9,expected_snapshot_version:2,loss_rate:"0.03",die_fee:0,plate_fee:0,freight_fee:0,other_fee:0}},showToast(message){{messages.push(message);}},openOrderDetail(...args){{opens.push(args);return true;}}}};
const makeSave=(initialActive,initialGeneration)=>{{globalThis.modalA11ySessionActive=initialActive;globalThis.modalA11ySessionGeneration=initialGeneration;return {{save:new AsyncFunction({harness_save!r}).bind(vm),close(){{globalThis.modalA11ySessionActive=false;globalThis.modalA11ySessionGeneration+=1;}}}};}};
const session=makeSave(true,4);
(async()=>{{const pending=session.save();for(let index=0;index<10&&!resolvePost;index+=1)await new Promise(resolve=>setImmediate(resolve));if(!resolvePost)throw new Error("post did not start");session.close();resolvePost({{data:{{}}}});if(await pending!==true)throw new Error("save outcome");if(opens.length)throw new Error("closed session reopened");if(!messages.some(value=>value.includes("原订单详情已关闭")))throw new Error("manual refresh guidance missing");}})().catch(error=>{{console.error(error);process.exit(1);}});''',
        encoding="utf-8",
    )
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def test_delivery_rows_keep_one_primary_next_step_and_disclose_secondary_actions() -> None:
    deliveries = _between(
        INDEX,
        '<template v-else-if="activePage === \'deliveries\'">',
        '<template v-else-if="activePage === \'finance\'">',
    )
    assert 'class="btn small primary delivery-row-primary-action"' in deliveries
    assert "deliveryPrimaryRowActionLabel(row)" in deliveries
    assert "runDeliveryPrimaryRowAction(row)" in deliveries
    assert 'class="delivery-row-more"' in deliveries
    assert 'popover="auto"' in deliveries
    assert '@click="toggleDeliveryRowMore(row,$event)"' in deliveries
    for condition in (
        "canDelivery && row.status==='pending'",
    ):
        assert condition in deliveries
    primary_logic = _between(INDEX, "deliveryPrimaryRowActionLabel(row) {", "runDeliveryPrimaryRowAction(row) {")
    assert 'return "拿货"' not in primary_logic
    assert 'return "发货打印"' in primary_logic
    assert 'return "打印"' in primary_logic
    delegate_logic = _between(INDEX, "runDeliveryPrimaryRowAction(row) {", "toggleDeliveryRowMore(row, event) {")
    assert "return this.dispatchDelivery(row)" in delegate_logic
    for action in ("deleteDelivery(row)", "cancelDelivery(row)", "cancelReceipt(row)"):
        assert action in deliveries


def test_delivery_primary_action_matrix_preserves_existing_business_methods(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    labels = _between(INDEX, "deliveryPrimaryRowActionLabel(row) {", "runDeliveryPrimaryRowAction(row) {").split("{", 1)[1].rsplit("}", 1)[0]
    delegate = _between(INDEX, "runDeliveryPrimaryRowAction(row) {", "toggleDeliveryRowMore(row, event) {").split("{", 1)[1].rsplit("}", 1)[0]
    target = tmp_path / "candidate-d-delivery-primary.js"
    target.write_text(
        f'''const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const label=new AsyncFunction("row",{labels!r});
const run=new AsyncFunction("row",{delegate!r});
const calls=[];
const vm={{canDelivery:true,canFinance:false,deliveryOperationState:{{action:"",deliveryId:null}},receiptOperationState:{{action:"",deliveryId:null}},
createDeliveryPickTask(row){{calls.push(["pick",row.id]);return "pick";}},dispatchDelivery(row){{calls.push(["dispatch",row.id]);return "dispatch";}},printDelivery(row){{calls.push(["print",row.id]);return "print";}}}};
(async()=>{{
if(await label.call(vm,{{id:1,status:"pending",pick_task:null}})!=="发货打印")throw new Error("no-task primary");
if(await run.call(vm,{{id:1,status:"pending",pick_task:null}})!=="dispatch")throw new Error("no-task delegate");
if(await label.call(vm,{{id:2,status:"pending",pick_task:{{id:9}}}})!=="发货打印")throw new Error("ready primary");
if(await run.call(vm,{{id:2,status:"pending",pick_task:{{id:9}}}})!=="dispatch")throw new Error("ready delegate");
vm.canDelivery=false;vm.canFinance=true;
if(await label.call(vm,{{id:3,status:"dispatched",return_receipt_status:"waiting_receipt"}})!=="打印")throw new Error("receipt primary");
if(await run.call(vm,{{id:3,status:"dispatched"}})!=="print")throw new Error("receipt delegate");
if(calls.map(row=>row[0]).join(",")!=="dispatch,dispatch,print")throw new Error("unexpected business method");
}})().catch(error=>{{console.error(error);process.exit(1);}});''',
        encoding="utf-8",
    )
    result = subprocess.run([node, str(target)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def test_destructive_delivery_confirmations_name_customer_and_document() -> None:
    delete_body = _between(INDEX, "async deleteDelivery(row) {", "async cancelDelivery(row) {")
    cancel_body = _between(INDEX, "async cancelDelivery(row) {", "async cancelReceipt(row) {")
    receipt_body = _between(
        INDEX,
        "async cancelReceipt(row) {",
        'exportStatement(row, format="xlsx")',
    )
    for body in (delete_body, cancel_body, receipt_body):
        assert "customerName" in body
        assert "deliveryNumber" in body
        assert 'String(row?.customer_name || "客户未登记")' in body
    assert "必填原因" not in delete_body + cancel_body + receipt_body
    receipt_modal = _between(INDEX, "<div v-else-if=\"modal.type === 'receipt'\">", "<div v-else-if=\"modal.type === 'deliveryRoute'\">")
    assert "receiptForm.customer_name" in receipt_modal
    assert "receiptForm.delivery_number" in receipt_modal
    open_receipt = _between(INDEX, "async openReceipt(row) {", "async saveReceipt() {")
    assert "customer_name:delivery.customer_name" in open_receipt
    assert "delivery_number:delivery.delivery_number" in open_receipt


def test_target_sizes_are_scoped_to_high_frequency_and_mobile_controls() -> None:
    assert ".ui-standard .high-frequency-action" in INDEX
    assert ".ui-standard .high-risk-action" in INDEX
    assert ".ui-large .business-flow-step { min-height:48px" in INDEX
    assert ".ui-large .delivery-flow-step { min-height:48px" in INDEX
    assert ".retry, .small-btn" in MOBILE
    assert "min-height: 44px" in _between(MOBILE, ".retry, .small-btn", ".entry-grid")
    assert ".map-btn" in MOBILE and "min-height: 44px" in _between(MOBILE, ".map-btn", ".periods")
    assert ".period-btn" in MOBILE and "min-height: 44px" in _between(MOBILE, ".period-btn", ".station-tabs")
    assert ".drawing-btn" in MOBILE and "min-height: 44px" in _between(MOBILE, ".drawing-btn", ".bottom-nav")


def test_candidate_d_keeps_current_release_external_acceptance_gate() -> None:
    # Candidate D's concrete accessibility contracts are asserted above.  Its
    # historical release-note wording must not pin every later release name;
    # the current version exposes the durable external-acceptance gate instead.
    assert "APP_VERSION = " in VERSION
    assert "APP_VERSION_NAME = " in VERSION
    assert "APP_EXTERNAL_ACCEPTANCE_REQUIRED = True" in VERSION


def test_index_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "candidate-d-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

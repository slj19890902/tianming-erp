import os
import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
TIME_UTILS = (ROOT / "static" / "assets" / "time-utils.js").read_text(
    encoding="utf-8"
)


def test_shared_inventory_order_payload_uses_idempotent_client_lines() -> None:
    assert "client_line_id: item.client_line_id || createIdempotencyKey()" in INDEX
    assert "reservation_plan: this.buildReservationPlan(item)" in INDEX
    assert "idempotency_key:crypto.randomUUID()" not in INDEX


def test_quantity_input_debounces_inventory_candidate_refresh() -> None:
    assert '@input="onOrderDraftQuantityInput(item)"' in INDEX
    assert '@input="invalidateImportDraftConfirmation(draft); scheduleOrderLineInventoryRefresh(item,draft.matched_customer_id)"' in INDEX
    assert "scheduleOrderLineInventoryRefresh(line, customerId)" in INDEX
    assert "line._inventory_refresh_timer = setTimeout" in INDEX


def test_pending_requisition_explains_semi_deduction_and_purchase_shortage() -> None:
    assert "需求 / 客户备料 / 采购" in INDEX
    assert "客户备料已预占：{{ row.semi_finished_reserved_piece_qty || 0 }} 个" in INDEX
    assert "仍需生产：{{ row.remaining_required_piece_qty || 0 }} 个" in INDEX
    assert "本次只需报：{{ row.requisition_qty || 0 }} 张" in INDEX


def test_supplier_draft_rechecks_late_semi_inventory_before_purchase() -> None:
    assert "发现可抵扣半成品" in INDEX
    assert "条可抵扣半成品库存" in INDEX
    assert "确认抵扣并重算采购" in INDEX
    assert "本次不用库存" in INDEX
    assert "/api/requisition/semi-inventory/reserve-from-pending" in INDEX
    assert "this.supplierRequisitionSelections = selections;" in INDEX
    assert "refreshSupplierRequisitionDraftAfterSemiReservation" in INDEX
    assert "发现可抵扣半成品库存，请先确认抵扣或选择本次不用库存" in INDEX
    assert "我已核对换算差异，同意本次匹配并记忆" in INDEX
    assert "按上次人工匹配推荐" in INDEX
    assert "首次人工匹配" in INDEX
    assert "candidate.lot_number" in INDEX
    assert "inventoryLocation(candidate)" in INDEX
    assert "supplierDraftSemiInventoryOptions()" in INDEX
    assert "[...recommended, ...review]" in INDEX
    assert ".slice(0, 1)" not in INDEX[INDEX.index("draftSemiInventoryCandidates(option)"):INDEX.index("draftSemiInventoryNeedsOverride(option)")]


def test_supplier_draft_keeps_recommended_and_review_lots_visible() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for supplier draft candidate test"
    script = next(
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL)
        if script.strip()
    )
    harness = f"""
const vm = require("vm");
const sandbox = {{
  axios: {{ defaults: {{}}, interceptors: {{ response: {{ use() {{}} }} }} }},
  Vue: {{ createApp(definition) {{ sandbox.definition = definition; return {{ component() {{ return this; }}, mount() {{ return this; }} }}; }} }},
  localStorage: {{ getItem() {{ return ""; }}, setItem() {{}}, removeItem() {{}} }},
  window: {{}}, console, URLSearchParams, setTimeout, clearTimeout,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(script)}, sandbox);
const method = sandbox.definition.methods.draftSemiInventoryCandidates;
const option = {{
  recommended_candidates:[{{lot_id:1,lot_number:"SI-RECOMMENDED"}}],
  review_candidates:[{{lot_id:2,lot_number:"SI-20260710-40CB3801AD"}},{{lot_id:1,lot_number:"DUPLICATE"}}],
}};
const result = method(option).map(row => row.lot_number);
if (JSON.stringify(result) !== JSON.stringify(["SI-RECOMMENDED","SI-20260710-40CB3801AD"])) throw new Error(JSON.stringify(result));
"""
    result = subprocess.run(
        [node], input=harness, text=True, encoding="utf-8", capture_output=True,
        env=os.environ.copy(), check=False,
    )
    assert result.returncode == 0, result.stderr


def test_warehouse_exposes_multi_product_assignment_and_mold_location() -> None:
    assert "分配 / 取消成品款号" in WAREHOUSE
    assert "/product-assignments?limit=500" in WAREHOUSE
    assert "保存款号分配" in WAREHOUSE
    assert "模具 / 货架位置" in WAREHOUSE
    assert "同一半成品规格的新旧批次共用此分配记忆" not in WAREHOUSE
    assert "data.binding_scope" in WAREHOUSE
    assert "模具与位置查询" in WAREHOUSE
    assert "/api/warehouse/molds" in WAREHOUSE
    assert "保存模具" in WAREHOUSE
    assert "productForm.mold_tool_id" in INDEX
    assert "mold_tool_id: f.mold_tool_id" in INDEX


def test_semi_lot_uses_one_admin_batch_editor() -> None:
    actions = WAREHOUSE.split("function actionButtons(row)", 1)[1].split(
        "function renderSemiLotEditor", 1
    )[0]
    assert 'state.user.role==="admin"' in actions
    assert "openSemiLotEditor" in actions
    assert "assignCustomer-" not in actions
    for marker in (
        "编辑半成品库存批次",
        "/edit-semi-finished",
        "customer_id:customerId",
        "customer_id:null",
        "expected_version:row.version",
        "指定客户后，至少绑定 1 个款号才能参与抵扣",
        "这会清空当前批次允许款号",
    ):
        assert marker in WAREHOUSE


def test_semi_lot_keeps_replace_checkbox_and_audited_void_path() -> None:
    assignment = WAREHOUSE.split("async function saveProductAssignments()", 1)[1].split(
        "async function operate", 1
    )[0]
    assert "product_ids:[...state.assignment.selected]" in assignment
    assert "state.assignment.selected.delete(productId)" in WAREHOUSE
    void = WAREHOUSE.split("async function voidSemiLot()", 1)[1].split(
        "async function openProductAssignments", 1
    )[0]
    for marker in ("/void-semi-finished", "expected_version:row.version", "reason:reason.trim()", "作废关闭并保留原流水"):
        assert marker in void


def test_lot_binding_uses_physical_batch_contract_and_refreshes_conflicts() -> None:
    assert "仅当前物理批次允许款号" in WAREHOUSE
    assert "binding_scope" in WAREHOUSE
    assert "allowed_product_ids" in WAREHOUSE
    assert "expected_version:state.assignment.version" in WAREHOUSE
    assert "err.status===409" in WAREHOUSE
    assert "await openProductAssignments(lotId,isDedicated)" in WAREHOUSE
    assert "同一半成品规格的新旧批次共用此分配记忆" not in WAREHOUSE
    assert "取消归属不会删除该客户共用的匹配记忆" not in WAREHOUSE
    assert "当前不可抵扣" in WAREHOUSE
    assert "通用批次：按物理规格人工确认" in WAREHOUSE


def test_general_semi_finished_source_is_manual_only_and_not_auto_selected() -> None:
    assert "general_signature" in INDEX
    assert "GENERAL_SEMI_FINISHED_STOCK" in INDEX
    assert "通用半成品（general_signature）" in INDEX
    assert "通用半成品，可跨客户，需人工确认" in INDEX
    assert "general_confirmation" in INDEX
    assert "明确确认并抵扣" in INDEX
    assert "黄色人工确认" in INDEX
    assert "不自动扣" in INDEX
    assert 'candidate.source === "general_signature"' in INDEX
    assert "inventoryCandidateNeedsManualConfirmation(candidate)" in INDEX


def test_inline_javascript_is_syntax_valid() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for inline JavaScript syntax checks"
    for html_path, html in ((ROOT / "static" / "index.html", INDEX), (ROOT / "static" / "warehouse.html", WAREHOUSE)):
        scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL) if script.strip()]
        assert scripts, f"{html_path} has no inline script"
        for script in scripts:
            result = subprocess.run(
                [node, "--check"],
                input=script,
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=os.environ.copy(),
                check=False,
            )
            assert result.returncode == 0, f"{html_path}: {result.stderr}"


def test_finished_then_semi_requirement_and_yield_allocation_are_explicit() -> None:
    assert "Math.max(Number(line.quantity || 0) - finishedQty, 0)" in INDEX
    assert "* this.inventoryCandidatePayload(line, component).pieces_per_box" in INDEX
    assert "Math.max(available - usedStock, 0) * yieldFactor" in INDEX
    assert "Math.ceil(allocated / yieldFactor)" in INDEX
    assert "usedStock + stock" in INDEX


def test_shared_lot_is_reallocated_in_line_order_with_yield() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend allocation test"
    script = next(script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL) if script.strip())
    harness = f"""
const vm = require("vm");
const sandbox = {{
  axios: {{ defaults: {{}}, interceptors: {{ response: {{ use() {{}} }} }} }},
  Vue: {{ createApp(definition) {{ sandbox.definition = definition; return {{ component() {{ return this; }}, mount() {{ return this; }} }}; }} }},
  localStorage: {{ getItem() {{ return ""; }}, setItem() {{}}, removeItem() {{}} }},
  window: {{}}, console, URLSearchParams, setTimeout, clearTimeout,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(TIME_UTILS)}, sandbox);
vm.runInContext({json.dumps(script)}, sandbox);
const method = sandbox.definition.methods.reallocateDraftInventorySequentially;
function candidate(lotId, stock, yieldFactor=1, finished=false, source="signature", differences=[]) {{ return {{ lot_id:lotId, version:1, source, match_rule_id:source === "learned" ? 42 : null, signature_differences:differences, available_stock_quantity:stock, quantity_available:finished ? stock : undefined, stock_yield_per_sheet:yieldFactor }}; }}
function part(candidates=[]) {{ return {{ candidates, manual_candidates:[], selected:candidates[0] || null, selected_candidates:candidates, allocations:[], skipped:false, unavailable_reason:"" }}; }}
function line(quantity, semiCandidates=[], finishedCandidates=[]) {{ return {{ quantity, _inventory:{{ api_error:false, stale:false, finished:part(finishedCandidates), semi:{{ whole:part(semiCandidates), cover:part(), base:part() }} }} }}; }}
const methods = sandbox.definition.methods;
const context = {{ inventoryPlanApplies:methods.inventoryPlanApplies, inventoryComponents: () => ["whole"], inventoryCandidatePayload: () => ({{ pieces_per_box:1 }}), semiCandidateNeedsOverride:methods.semiCandidateNeedsOverride, inventoryCandidateWarnings:methods.inventoryCandidateWarnings, isGeneralSemiFinishedCandidate:methods.isGeneralSemiFinishedCandidate }};
const shared = [line(50,[candidate(7,100)]), line(30,[candidate(7,100)]), line(30,[candidate(7,100)])];
method.call(context, shared);
const yielded = [line(1,[candidate(8,2,3)]), line(5,[candidate(8,2,3)])];
method.call(context, yielded);
const multi = [line(7,[candidate(9,3,1,false,"learned",["材质不同"]),candidate(10,4)])];
method.call(context, multi);
const plan = methods.buildReservationPlan.call(context, multi[0]);
const manual = [line(2,[candidate(12,2,1,false,"manual",["尺寸不同"])])];
method.call(context, manual);
const manualPlan = methods.buildReservationPlan.call(context, manual[0]);
const finished = [line(4,[],[candidate(11,5,1,true)]), line(4,[],[candidate(11,5,1,true)])];
method.call(context, finished);
const decisionContext = {{ inventoryPlanApplies:methods.inventoryPlanApplies, inventoryComponents:() => ["whole"], inventoryStateMatchesLine:() => true, componentLabel:methods.componentLabel }};
const apiError = line(1); apiError._inventory.api_error = true; apiError._inventory.error = "库存候选加载失败：网络错误";
const incomplete = line(1); incomplete._inventory.semi.whole.unavailable_reason = "常用箱缺少报料尺寸/材质/楞型，无法推荐半成品";
const apiBlocked = methods.inventoryDecisionRequired.call(decisionContext, apiError);
const incompleteBlocked = methods.inventoryDecisionRequired.call(decisionContext, incomplete);
incomplete._inventory.semi.whole.skipped = true;
const incompleteSkipped = methods.inventoryDecisionRequired.call(decisionContext, incomplete);
const result = {{
  shared:shared.map(row => row._inventory.semi.whole.allocations.reduce((sum,a) => sum+a.requested_qty,0)),
  yielded:yielded.map(row => row._inventory.semi.whole.allocations.reduce((sum,a) => sum+a.requested_qty,0)),
  yieldedStock:yielded.reduce((sum,row) => sum+row._inventory.semi.whole.allocations.reduce((n,a) => n+a.stock_quantity,0),0),
  multiPlan:plan.semi.map(entry => entry.requested_qty),
  multiWarnings:plan.semi.map(entry => [entry.override,entry.warning_acknowledged_codes]),
  manualWarning:[manualPlan.semi[0].override,manualPlan.semi[0].warning_acknowledged_codes],
  finished:finished.map(row => row._inventory.finished.allocations.reduce((sum,a) => sum+a.requested_qty,0)),
  gates:[!!apiBlocked,!!incompleteBlocked,incompleteSkipped],
}};
const expected = {{shared:[50,30,20],yielded:[1,3],yieldedStock:2,multiPlan:[3,4],multiWarnings:[[true,["SEMI_SIGNATURE_OVERRIDE"]],[false,[]]],manualWarning:[true,["SEMI_SIGNATURE_OVERRIDE"]],finished:[4,1],gates:[true,true,""]}};
if (JSON.stringify(result) !== JSON.stringify(expected)) throw new Error(JSON.stringify(result));
let removeReallocations = 0;
const removeContext = {{orderForm:{{items:[{{}},{{}}]}},modal:null,reallocateAllDraftInventory() {{ removeReallocations += 1; }}}};
methods.removeOrderItem.call(removeContext,0);
if (removeContext.orderForm.items.length !== 1 || removeReallocations !== 1) throw new Error("removing a line did not reallocate inventory");
(async () => {{
  const requests = [];
  sandbox.axios.get = async (url) => {{ requests.push(url); return url.includes("/api/master/products/") ? {{data:{{id:99,report_length_mm:10,report_width_mm:20,material_code:"C4C",flute_type:"B",pieces_per_box:1}}}} : {{data:{{items:[]}}}}; }};
  sandbox.axios.post = async (url) => {{ requests.push(url); return {{data:{{items:[]}}}}; }};
  const fallbackLine = {{matched_product_id:99,quantity:2}};
  const loadContext = {{
    inventoryPlanApplies:methods.inventoryPlanApplies,
    inventoryCustomerForLine:() => 5, newOrderInventoryState:methods.newOrderInventoryState,
    inventoryComponents:() => ["whole"], inventoryCandidatePayload:() => ({{board_length_mm:10,board_width_mm:20,material_code:"C4C",flute_type:"B"}}),
    inventoryPayloadUnavailableReason:methods.inventoryPayloadUnavailableReason, componentLabel:methods.componentLabel,
    errorMessage:error => error.message, reallocateAllDraftInventory() {{}},
  }};
  await methods.loadOrderLineInventory.call(loadContext, fallbackLine);
  if (fallbackLine._inventory.context.customer_id !== 5 || requests.length !== 3) throw new Error("PDF default customer was not used");

  const confirmedState = multi[0]._inventory;
  confirmedState.context = {{product_id:99,customer_id:5,quantity:7}};
  let reloads = 0;
  const draft = {{matched_customer_id:5,order_date:"2026-07-17",delivery_date:"2026-07-24",items:[{{matched_product_id:99,quantity:7,unit_price:"1",product_name:"PDF产品",_inventory:confirmedState,_inventory_product:{{id:99}},client_line_id:"pdf-line"}}]}};
  const pdfContext = {{
    orderImportDrafts:[draft], orderForm:{{items:[]}}, newOrderInventoryState:methods.newOrderInventoryState,
    isImportDraftLocked:() => false,
    inventoryStateMatchesLine:methods.inventoryStateMatchesLine, searchOrderProducts:async () => {{}},
    loadOrderDraftBom:async () => {{}},
    loadOrderLineInventory:async () => {{ reloads += 1; }}, reallocateAllDraftInventory() {{}}, refreshOrderNumberPreview:async () => {{}},
  }};
  await methods.applyPdfDraftToOrderForm.call(pdfContext, draft);
  const preservedPlan = methods.buildReservationPlan.call(context, pdfContext.orderForm.items[0]);
  if (reloads || JSON.stringify(preservedPlan.semi.map(entry => entry.requested_qty)) !== JSON.stringify([3,4])) throw new Error("PDF confirmed plan was not preserved");
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(
        [node], input=harness, text=True, encoding="utf-8", capture_output=True,
        env=os.environ.copy(), check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pdf_customer_fallback_and_confirmed_state_preservation_are_explicit() -> None:
    assert INDEX.count("customerId = Number(customerId || this.inventoryCustomerForLine(line));") == 2
    select_start = INDEX.index("async selectImportProduct(draft, item)")
    select_end = INDEX.index("pdfItemMaterialText(item)", select_start)
    assert "this.refreshOrderLineInventory(item);" in INDEX[select_start:select_end]
    apply_start = INDEX.index("async applyPdfDraftToOrderForm")
    apply_end = INDEX.index("async saveConfirmedImportDrafts", apply_start)
    source = INDEX[apply_start:apply_end]
    assert "!this.inventoryStateMatchesLine(this.orderForm.items[index], this.orderForm.customer_id)" in source


def test_api_failures_block_but_incomplete_signature_can_be_skipped() -> None:
    assert "state.api_error = failures.length > 0" in INDEX
    assert "if (state.api_error) return state.error" in INDEX
    assert "inventoryPayloadUnavailableReason(payload)" in INDEX
    assert "常用箱缺少报料尺寸/材质/楞型，无法推荐半成品" in INDEX
    assert "if (reason) state.semi[component].unavailable_reason = reason" in INDEX
    assert "if (part.unavailable_reason && !part.skipped)" in INDEX
    assert "if (state.semi[component].unavailable_reason) state.semi[component].skipped = true" in INDEX


def test_multi_lot_plans_and_zero_allocations_use_allocation_records() -> None:
    assert "state.finished.allocations.map" in INDEX
    assert "state.semi[component].allocations.map" in INDEX
    assert "推荐批次已无可分配库存" in INDEX
    assert "采用安全推荐" in INDEX
    assert "系统已安排" in INDEX


def test_semi_plan_warnings_and_line_removal_reallocation_are_explicit() -> None:
    assert 'const warningAcknowledgedCodes = []' in INDEX
    assert 'if (general && part.general_confirmation) warningAcknowledgedCodes.push("GENERAL_SEMI_FINISHED_STOCK")' in INDEX
    assert 'confirmed:!general || part.general_confirmation' in INDEX
    assert '"GENERAL_SEMI_FINISHED_STOCK"' in INDEX
    assert 'candidate?.source === "learned"' in INDEX
    assert 'const recommendationSource = candidate.recommendation_source || candidate.source || "signature"' in INDEX
    assert "差异警告" in INDEX
    start = INDEX.index("removeOrderItem(index)")
    end = INDEX.index("async searchOrderProducts", start)
    assert "this.orderForm.items.splice(index,1);" in INDEX[start:end]
    assert "this.reallocateAllDraftInventory();" in INDEX[start:end]


def test_a3_components_and_unconfirmed_candidate_block_are_explicit() -> None:
    assert '["cover", "base"]' in INDEX
    assert "存在成品库存候选，请确认抵扣或选择本次不用库存。" in INDEX
    assert "存在半成品${this.componentLabel(component)}候选，请确认抵扣或选择本次不用库存。" in INDEX
    assert "confirmed:true" in INDEX


def test_pdf_direct_save_carries_the_same_reservation_plan() -> None:
    start = INDEX.index("async saveConfirmedImportDrafts()")
    end = INDEX.index("openOrderEditor(group)", start)
    source = INDEX[start:end]
    assert "inventoryDecisionRequired(item)" in source
    assert "client_line_id: item.client_line_id || createIdempotencyKey()" in source
    assert "reservation_plan: this.buildReservationPlan(item)" in source


def test_new_and_pdf_order_quantity_cells_share_automatic_inventory_summary() -> None:
    assert 'class="btn small success inventory-recommend-button"' not in INDEX
    assert '@click="confirmSafeOrderLineInventoryRecommendations(item)"' not in INDEX
    assert INDEX.count("orderLineInventoryAutoSummary(item)") == 2
    assert INDEX.count("orderLineInventoryNeedsAttention(item)") >= 4
    assert INDEX.count("查看库存安排") == 2
    assert "下单${orderQuantity}" in INDEX
    assert "现有成品${availableFinished}" in INDEX
    assert "自动预占${reservedFinished}" in INDEX
    assert "需生产${productionRequired}" in INDEX
    assert '@input="onOrderDraftQuantityInput(item)"' in INDEX
    assert '@input="invalidateImportDraftConfirmation(draft); scheduleOrderLineInventoryRefresh(item,draft.matched_customer_id)"' in INDEX
    load = INDEX.split("async loadOrderLineInventory(line, customerId) {", 1)[1].split(
        "async loadOrderLineManualInventory", 1
    )[0]
    assert "this.confirmSafeOrderLineInventoryRecommendations?.(line);" in load
    confirm = INDEX.split("confirmOrderLineInventory(line, component", 1)[1].split(
        "skipOrderLineInventory", 1
    )[0]
    assert "confirm(" not in confirm
    assert "safeSystemInventoryCandidates(line, component)" in confirm
    assert INDEX.count("inventoryLocation(candidate)") >= 4
    assert INDEX.count("inventoryLocation(allocation.candidate)") >= 4


def test_safe_inventory_recommendation_one_click_excludes_risky_candidates() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for safe inventory recommendation test"
    script = next(
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL)
        if script.strip()
    )
    harness = f"""
const vm = require("vm");
const sandbox = {{
  axios: {{ defaults: {{}}, interceptors: {{ response: {{ use() {{}} }} }} }},
  Vue: {{ createApp(definition) {{ sandbox.definition = definition; return {{ component() {{ return this; }}, mount() {{ return this; }} }}; }} }},
  localStorage: {{ getItem() {{ return ""; }}, setItem() {{}}, removeItem() {{}} }},
  window: {{}}, console, URLSearchParams, setTimeout, clearTimeout,
  confirm() {{ throw new Error("browser confirm must not run"); }},
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(script)}, sandbox);
const methods = sandbox.definition.methods;
const part = candidates => ({{candidates,manual_candidates:[],selected:null,selected_candidates:[],allocations:[],skipped:false,manual_override:false,unavailable_reason:""}});
const dedicated = {{lot_id:1,quantity_available:5,warning_codes:[]}};
const general = {{lot_id:2,quantity_available:5,is_general:true,warning_codes:[]}};
const warningAlias = {{lot_id:7,quantity_available:5,warning_codes:[],warning_messages:[],warnings:["OWNER_REVIEW"]}};
const safeSemi = {{lot_id:3,source:"signature",available_stock_quantity:20,signature_differences:[],warning_codes:[]}};
const learnedDifference = {{lot_id:4,source:"learned",available_stock_quantity:20,signature_differences:["material_code"],warning_codes:["SEMI_SIGNATURE_OVERRIDE"]}};
const accidentalManual = {{lot_id:5,source:"manual",available_stock_quantity:20,signature_differences:[],warning_codes:[]}};
const line = {{product_id:99,quantity:10,_inventory:{{loading:false,stale:false,api_error:false,context:{{product_id:99,customer_id:7,quantity:10}},finished:part([dedicated,general,warningAlias]),semi:{{whole:part([safeSemi,learnedDifference,accidentalManual]),cover:part([]),base:part([])}}}}}};
let reallocations = 0;
const context = {{
  inventoryComponents:() => ["whole"], inventoryStateMatchesLine:() => true,
  semiCandidateNeedsOverride:methods.semiCandidateNeedsOverride,
  isGeneralSemiFinishedCandidate:methods.isGeneralSemiFinishedCandidate,
  inventoryCandidateWarnings:methods.inventoryCandidateWarnings,
  isSafeSystemInventoryCandidate:methods.isSafeSystemInventoryCandidate,
  safeSystemInventoryCandidates:methods.safeSystemInventoryCandidates,
  hasSafeOrderLineInventoryRecommendation:methods.hasSafeOrderLineInventoryRecommendation,
  reallocateAllDraftInventory() {{ reallocations += 1; }},
  showToast(message, error) {{ throw new Error(`unexpected toast: ${{message}} / ${{error}}`); }},
}};
if (!methods.hasSafeOrderLineInventoryRecommendation.call(context,line)) throw new Error("safe recommendation button should be visible");
methods.confirmSafeOrderLineInventoryRecommendations.call(context,line);
const finishedIds = line._inventory.finished.selected_candidates.map(row => row.lot_id);
const semiIds = line._inventory.semi.whole.selected_candidates.map(row => row.lot_id);
if (JSON.stringify(finishedIds) !== JSON.stringify([1])) throw new Error(`unsafe finished candidate selected: ${{JSON.stringify(finishedIds)}}`);
if (JSON.stringify(semiIds) !== JSON.stringify([3])) throw new Error(`unsafe semi candidate selected: ${{JSON.stringify(semiIds)}}`);
if (reallocations !== 1) throw new Error(`expected one reallocation, got ${{reallocations}}`);
line._inventory.semi.whole.manual_override = true;
methods.confirmOrderLineInventory.call(context,line,"whole",{{lot_id:6,source:"manual",signature_differences:["尺寸"]}},true);
if (line._inventory.semi.whole.selected_candidates[0].lot_id !== 6 || reallocations !== 2) throw new Error("manual override path was not preserved");
"""
    result = subprocess.run(
        [node], input=harness, text=True, encoding="utf-8", capture_output=True,
        env=os.environ.copy(), check=False,
    )
    assert result.returncode == 0, result.stderr


def test_loading_inventory_automatically_selects_only_safe_candidates() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for automatic inventory recommendation test"
    script = next(
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL)
        if script.strip()
    )
    harness = f"""
const vm = require("vm");
const sandbox = {{
  axios: {{ defaults: {{}}, interceptors: {{ response: {{ use() {{}} }} }} }},
  Vue: {{ createApp(definition) {{ sandbox.definition = definition; return {{ component() {{ return this; }}, mount() {{ return this; }} }}; }} }},
  localStorage: {{ getItem() {{ return ""; }}, setItem() {{}}, removeItem() {{}} }},
  window: {{}}, console, URLSearchParams, setTimeout, clearTimeout,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(script)}, sandbox);
const methods = sandbox.definition.methods;
const dedicated = {{lot_id:11,version:3,quantity_available:8,warning_codes:[],warehouse_location:{{location_code:"E1-L09"}}}};
const generalSemi = {{lot_id:12,version:1,source:"general_signature",available_stock_quantity:20,warning_codes:["GENERAL_SEMI_FINISHED_STOCK"]}};
sandbox.axios.get = async url => url.includes("/api/master/products/")
  ? {{data:{{id:99,box_style:"A1",report_length_mm:100,report_width_mm:200,material_code:"C4C",flute_type:"B",pieces_per_box:1}}}}
  : {{data:{{items:[dedicated]}}}};
sandbox.axios.post = async () => ({{data:{{items:[generalSemi]}}}});
const line = {{product_id:99,quantity:5}};
let reallocations = 0;
const context = {{
  inventoryPlanApplies:methods.inventoryPlanApplies,
  inventoryCustomerForLine:() => 7,
  newOrderInventoryState:methods.newOrderInventoryState,
  inventoryComponents:() => ["whole"],
  inventoryCandidatePayload:methods.inventoryCandidatePayload,
  inventoryPayloadUnavailableReason:methods.inventoryPayloadUnavailableReason,
  componentLabel:methods.componentLabel,
  errorMessage:error => error.message,
  inventoryStateMatchesLine:() => true,
  inventoryCandidateWarnings:methods.inventoryCandidateWarnings,
  semiCandidateNeedsOverride:methods.semiCandidateNeedsOverride,
  isSafeSystemInventoryCandidate:methods.isSafeSystemInventoryCandidate,
  safeSystemInventoryCandidates:methods.safeSystemInventoryCandidates,
  hasSafeOrderLineInventoryRecommendation:methods.hasSafeOrderLineInventoryRecommendation,
  confirmSafeOrderLineInventoryRecommendations:methods.confirmSafeOrderLineInventoryRecommendations,
  invalidatePdfDraftForItem() {{}},
  reallocateAllDraftInventory() {{ reallocations += 1; }},
}};
(async () => {{
  await methods.loadOrderLineInventory.call(context,line,7);
  const finishedIds = line._inventory.finished.selected_candidates.map(row => row.lot_id);
  const semiIds = line._inventory.semi.whole.selected_candidates.map(row => row.lot_id);
  if (JSON.stringify(finishedIds) !== JSON.stringify([11])) throw new Error(`safe finished inventory was not selected: ${{JSON.stringify(finishedIds)}}`);
  if (semiIds.length) throw new Error(`general semi-finished inventory must stay manual: ${{JSON.stringify(semiIds)}}`);
  if (reallocations < 1) throw new Error("automatic selection did not recalculate the plan");
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(
        [node], input=harness, text=True, encoding="utf-8", capture_output=True,
        env=os.environ.copy(), check=False,
    )
    assert result.returncode == 0, result.stderr


def test_general_semi_finished_requires_explicit_confirmation_before_payload_warning() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for general semi-finished confirmation test"
    script = next(
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL)
        if script.strip()
    )
    harness = f"""
const vm = require("vm");
const sandbox = {{
  axios: {{ defaults: {{}}, interceptors: {{ response: {{ use() {{}} }} }} }},
  Vue: {{ createApp(definition) {{ sandbox.definition = definition; return {{ component() {{ return this; }}, mount() {{ return this; }} }}; }} }},
  localStorage: {{ getItem() {{ return ""; }}, setItem() {{}}, removeItem() {{}} }},
  window: {{}}, console, URLSearchParams, setTimeout, clearTimeout,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(script)}, sandbox);
const methods = sandbox.definition.methods;
const general = {{lot_id:11,version:3,source:"general_signature",recommendation_source:"general_signature",available_stock_quantity:20,warning_codes:["GENERAL_SEMI_FINISHED_STOCK"]}};
const part = {{candidates:[general],manual_candidates:[],selected:null,selected_candidates:[],allocations:[],skipped:false,manual_override:false,general_confirmation:false,unavailable_reason:""}};
const line = {{product_id:99,quantity:5,_inventory:{{loading:false,stale:false,api_error:false,context:{{product_id:99,customer_id:7,quantity:5}},finished:{{candidates:[],manual_candidates:[],selected:null,selected_candidates:[],allocations:[],skipped:false}},semi:{{whole:part,cover:{{candidates:[],manual_candidates:[],selected_candidates:[],allocations:[],skipped:false}},base:{{candidates:[],manual_candidates:[],selected_candidates:[],allocations:[],skipped:false}}}}}}}};
let reallocations = 0;
const context = {{
  inventoryPlanApplies:methods.inventoryPlanApplies,
  inventoryComponents:() => ["whole"],
  inventoryCandidateWarnings:methods.inventoryCandidateWarnings,
  isGeneralSemiFinishedCandidate:methods.isGeneralSemiFinishedCandidate,
  semiCandidateNeedsOverride:methods.semiCandidateNeedsOverride,
  reallocateAllDraftInventory() {{ reallocations += 1; }},
  showToast() {{ throw new Error("unexpected toast"); }},
}};
methods.confirmOrderLineInventory.call(context,line,"whole",general,true);
if (part.selected_candidates.length || reallocations) throw new Error("general candidate was confirmed without acknowledgement");
part.general_confirmation = true;
methods.confirmOrderLineInventory.call(context,line,"whole",general,true);
if (part.selected_candidates[0] !== general || reallocations !== 1) throw new Error("general candidate was not explicitly confirmed");
part.allocations = [{{candidate:general,requested_qty:5,stock_quantity:1}}];
const plan = methods.buildReservationPlan.call(context,line).semi[0];
if (plan.recommendation_source !== "general_signature" || plan.confirmed !== true || JSON.stringify(plan.warning_acknowledged_codes) !== JSON.stringify(["GENERAL_SEMI_FINISHED_STOCK"])) throw new Error(JSON.stringify(plan));
part.general_confirmation = false;
const unconfirmed = methods.buildReservationPlan.call(context,line).semi[0];
if (unconfirmed.confirmed !== false || unconfirmed.warning_acknowledged_codes.includes("GENERAL_SEMI_FINISHED_STOCK")) throw new Error(JSON.stringify(unconfirmed));
"""
    result = subprocess.run(
        [node], input=harness, text=True, encoding="utf-8", capture_output=True,
        env=os.environ.copy(), check=False,
    )
    assert result.returncode == 0, result.stderr


def test_safe_inventory_gate_covers_general_warning_difference_and_manual_sources() -> None:
    gate = INDEX.split("isSafeSystemInventoryCandidate(component, candidate)", 1)[1].split(
        "safeSystemInventoryCandidates", 1
    )[0]
    for marker in (
        'candidate.source === "manual"',
        "candidate.requires_confirmation",
        "inventoryCandidateWarnings(candidate)",
        "candidate.is_general",
        "candidate.signature_differences",
        "semiCandidateNeedsOverride(candidate)",
    ):
        assert marker in gate


def test_delivery_inventory_sources_are_screen_only() -> None:
    assert "inventory_sources" in INDEX
    print_method = re.search(r"printDelivery\(row\) \{(?P<body>[^}]*)\}", INDEX)
    assert print_method is not None
    assert "inventory_sources" not in print_method.group("body")


def test_inline_script_passes_node_check(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start : INDEX.index(end_marker, start)]


def test_delivery_guide_is_short_reuses_existing_actions_and_never_auto_executes() -> None:
    guide = _block(
        '<div class="delivery-flow-guide"',
        '<div class="form-grid">',
    )
    guide_logic = _block(
        "deliveryGuideStage() {",
        "deliveryCustomerOptions() {",
    )
    for label in ("选客户", "选待送货", "核对数量", "保存", "打印发货"):
        assert label in guide_logic
    assert "deliveryGuideSteps" in guide
    assert "deliveryGuideStepIndex" in guide
    assert "@click" not in guide
    assert "axios" not in guide
    assert "/api/" not in guide
    assert '<button' not in guide

    modal = _block(
        "<div v-else-if=\"modal.type === 'delivery'\">",
        "<div v-else-if=\"modal.type === 'tianhuaPreimport'\">",
    )
    assert "delivery-guide-target" in modal
    assert 'deliveryGuideStage === \'customer\'' in modal
    assert 'deliveryGuideStage === \'items\'' in modal
    assert "delivery-guide-review-target" in modal
    assert "toggleDeliveryBatchPicker()" in modal
    assert "toggleUnorderedFinishedPicker" in modal

    footer = _block('<div v-if="modal?.type !== \'product\'" class="modal-foot">', "<!-- 共用图纸预览")
    assert "deliveryPrimaryAction()" in footer
    assert "deliveryPrimaryLabel()" in footer
    assert "['review','save','print'].includes(deliveryGuideStage)" in footer


def test_pending_delivery_view_points_to_customer_without_creating_a_second_flow() -> None:
    page = _block(
        '<template v-else-if="activePage === \'deliveries\'">',
        '<template v-else-if="activePage === \'finance\'">',
    )
    assert "下一步：选客户开送货单" in page
    assert "点客户后直接进入送货明细" in page
    assert 'class="btn primary" @click="openDelivery(group.customer_id)"' in page
    assert page.count("@click=\"openDelivery(group.customer_id)\"") == 1


def test_delivery_guide_layout_keeps_five_steps_without_horizontal_scrolling() -> None:
    styles = _block(".delivery-flow-guide {", ".order-page-actionbar")
    assert "grid-template-columns:repeat(5,minmax(0,1fr))" in styles
    assert "text-overflow:ellipsis" in styles
    assert "overflow-x:auto" not in styles
    assert "overflow-x:scroll" not in styles


def test_delivery_guide_stage_follows_real_form_state(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend behavior validation"
    scripts = [
        source
        for source in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL
        )
        if source.strip()
    ]
    assert len(scripts) == 1
    script_path = tmp_path / "p1-26g-index.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    harness_path = tmp_path / "p1-26g-harness.js"
    harness_path.write_text(
        r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const sandbox = {
  axios:{defaults:{},interceptors:{response:{use(){}}}},
  Vue:{createApp(definition){sandbox.definition=definition;return {component(){return this},mount(){return this}}}},
  localStorage:{getItem(){return ""},setItem(){},removeItem(){}},
  window:{},console,URLSearchParams,setTimeout,clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(source,sandbox);
const methods = sandbox.definition.methods;
const computed = sandbox.definition.computed;
const context = {
  modal:{type:"delivery"},
  deliveryForm:{editingId:null,customer_id:null,source_tab:"order",delivery_date:"2026-08-09",vehicle_number:"",lines:[],saved_signature:"",pick_task:null},
  deliverySaveState:{saving:false,outcomeUncertain:false},
  deliveryLineHasInput:methods.deliveryLineHasInput,
  deliverySourceModeFromLines:methods.deliverySourceModeFromLines,
  deliveryFormSignature:methods.deliveryFormSignature,
  deliveryFormHasUnsavedChanges:methods.deliveryFormHasUnsavedChanges,
  deliveryFormIsDirty:methods.deliveryFormIsDirty,
  validationError:"",
};
Object.defineProperty(context,"deliveryGuideStage",{get(){return computed.deliveryGuideStage.call(context)}});
Object.defineProperty(context,"deliveryValidationError",{get(){return context.validationError}});
function expectStage(expected) {
  const actual = computed.deliveryGuideStage.call(context);
  if (actual !== expected) throw new Error(`expected ${expected}, got ${actual}`);
}
expectStage("customer");
if (!computed.deliveryGuideMessage.call(context).includes("名称、缩写或拼音首字母")) throw new Error("customer hint missing");
context.deliveryForm.customer_id = 7;
expectStage("items");
context.deliveryForm.lines = [{source_type:"order",order_item_id:91,delivered_quantity:12,remarks:""}];
expectStage("review");
context.validationError = "第1行库存不足";
if (!computed.deliveryGuideMessage.call(context).includes("先处理：第1行库存不足")) throw new Error("validation hint missing");
context.validationError = "";
context.deliverySaveState.saving = true;
expectStage("save");
if (!computed.deliveryGuideMessage.call(context).includes("不要重复点击")) throw new Error("saving hint missing");
context.deliverySaveState.saving = false;
context.deliveryForm.editingId = 81;
context.deliveryForm.saved_signature = context.deliveryFormSignature();
expectStage("print");
if (!computed.deliveryGuideMessage.call(context).includes("可选“拿货”")) throw new Error("optional pick hint missing");
context.deliveryForm.vehicle_number = "苏E-UAT";
expectStage("save");
if (!computed.deliveryGuideMessage.call(context).includes("保存修改")) throw new Error("dirty draft hint missing");
context.deliverySaveState.outcomeUncertain = true;
if (!computed.deliveryGuideMessage.call(context).includes("不要重复提交")) throw new Error("uncertain outcome hint missing");
''',
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(harness_path), str(script_path)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

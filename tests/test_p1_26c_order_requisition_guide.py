from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"
INDEX = INDEX_PATH.read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start : INDEX.index(end_marker, start)]


def test_compact_business_flow_uses_five_plain_language_steps_without_false_completion() -> None:
    shell = _block(
        '<section v-if="businessFlowCurrentStep"',
        '<section v-if="orderNextStepGuide.visible',
    )
    computed = _block("businessFlowSteps() {", "currentReleaseDetails() {")

    labels = ["订单", "报料", "来料", "生产", "送货"]
    positions = [computed.index(f'label:"{label}"') for label in labels]
    assert positions == sorted(positions)
    assert len(positions) == 5
    assert 'label:"回单/对账"' not in computed
    assert '{ key: "finance", label: "对账与开票" }' in INDEX
    assert 'aria-current="businessFlowCurrentStep.key===step.key ? \'step\' : null"' in shell
    assert "completed" not in shell.lower()
    assert ".business-flow-steps { display:grid; grid-template-columns:repeat(5,minmax(0,1fr))" in INDEX


def test_order_success_guide_is_permission_scoped_and_keeps_copy_short() -> None:
    guide = _block(
        '<section v-if="orderNextStepGuide.visible',
        '<section v-if="warehouseFrameUrl || warehouseLedgerUrl"',
    )
    assert "下一步去报料，核对材质和报料数量。" in guide
    assert "下一步：去报料" in guide
    assert "继续录订单" in guide
    assert "canRequisition && pageAllowed('requisition')" in guide
    assert "请交给有报料权限的账号继续" in guide
    assert "确认是否" not in guide


def test_manual_and_pdf_order_success_share_one_next_step_guide() -> None:
    manual = _block("async saveNewOrder(orderPayload) {", "async saveCurrentOrderItem(")
    pdf = _block("async saveConfirmedImportDrafts(targetDraft=null) {", "async openOrderEditor(group) {")

    assert 'this.showOrderNextStepGuide({orderNo,count:1,source:"manual",customerId:payload.customer_id})' in manual
    assert 'this.showOrderNextStepGuide({count:succeeded.length,source:"pdf"})' in pdf
    assert "if (succeeded.length)" in pdf
    assert 'draft._save_status = unknown ? "unknown" : "failed"' in pdf
    assert "remainingFailed.length" in pdf


def test_next_step_action_only_opens_existing_pending_requisition_page(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend behavior validation"

    scripts = [
        source
        for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if source.strip()
    ]
    assert len(scripts) == 1
    script_path = tmp_path / "p1-26c-index.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    harness_path = tmp_path / "p1-26c-harness.js"
    harness_path.write_text(
        r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const sandbox = {
  axios:{defaults:{},interceptors:{response:{use(){}}}},
  Vue:{createApp(definition){sandbox.definition=definition;return {component(){return this},mount(){return this}}}},
  localStorage:{getItem(){return ""},setItem(){},removeItem(){}},
  window:{},TMOrderReference:{component:{}},console,URLSearchParams,setTimeout,clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(source,sandbox);
const methods = sandbox.definition.methods;
const calls = [];
const context = {
  ...methods,
  canRequisition:true,
  requisitionTab:"submitted",
  orderNextStepGuide:{visible:true,orderNo:"TM-UAT",count:1,source:"manual"},
  pageAllowed(page){return page === "requisition";},
  invalidatePageCache(page){calls.push(`invalidate:${page}`);},
  async go(page){calls.push(`go:${page}`);},
  showToast(message,isError){calls.push(`toast:${message}:${!!isError}`);},
};
(async () => {
  const result = await methods.goToRequisitionFromOrderGuide.call(context);
  if (!result) throw new Error("Authorized guide did not navigate");
  if (context.requisitionTab !== "pending") throw new Error("Guide did not select pending requisition");
  if (context.orderNextStepGuide.visible) throw new Error("Guide did not close after navigation");
  if (calls.join("|") !== "invalidate:requisition|go:requisition") throw new Error(calls.join("|"));
  const body = methods.goToRequisitionFromOrderGuide.toString();
  if (/axios\.|\/api\//.test(body)) throw new Error("Guide action must not write business data");
})().catch(error => { console.error(error); process.exitCode=1; });
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

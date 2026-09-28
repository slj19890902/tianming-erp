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


def test_requisition_success_guide_is_short_permission_scoped_and_receipt_safe() -> None:
    guide = _block(
        '<section v-if="requisitionNextStepGuide.visible',
        '<section v-if="warehouseFrameUrl || warehouseLedgerUrl"',
    )
    assert "材料到厂后，再去来料入库确认实收。" in guide
    assert "材料到厂后：去来料入库" in guide
    assert "继续报料" in guide
    assert "hasPermission('incoming.execute') && pageAllowed('incoming')" in guide
    assert "请交给有来料入库权限的账号确认实收" in guide
    assert "确认是否" not in guide
    assert "自动入库" not in guide


def test_all_three_formal_requisition_success_paths_show_the_same_guide() -> None:
    save_modal = _block(
        'if (this.modal.type === "stockReplenishment")',
        'if (this.modal.type === "delivery")',
    )
    assert save_modal.count("this.showRequisitionNextStepGuide({") == 3
    assert 'source:"stock_replenishment"' in save_modal
    assert 'source:"supplier"' in save_modal
    assert 'source:"composite"' in save_modal
    assert save_modal.count("if (data?._in_flight) return false;") == 3


def test_preview_waiting_void_and_uncertain_paths_do_not_claim_completion() -> None:
    preview = _block("async openSupplierRequisitionDraft(", "async openCompositeRequisition(")
    waiting = _block("async releaseRequisitionHold(", "requisitionHoldIsDue(")
    supplier_save = _block("async saveSupplierRequisitionDraft()", "supplierRequisitionSelectionSignature(")
    composite_save = _block("async saveCompositeRequisitionDraft()", "async saveStockReplenishmentDraft()")
    stock_save = _block("async saveStockReplenishmentDraft()", "async saveSupplierRequisitionDraft()")

    assert "showRequisitionNextStepGuide" not in preview
    assert "showRequisitionNextStepGuide" not in waiting
    assert "showRequisitionNextStepGuide" not in supplier_save
    assert "showRequisitionNextStepGuide" not in composite_save
    assert "showRequisitionNextStepGuide" not in stock_save
    assert "state.uncertain = true" in supplier_save
    assert "state.uncertain = true" in composite_save
    assert "state.uncertain = true" in stock_save


def test_incoming_guide_action_only_opens_existing_pending_receipt_page(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend behavior validation"

    scripts = [
        source
        for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if source.strip()
    ]
    assert len(scripts) == 1
    script_path = tmp_path / "p1-26d-index.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    harness_path = tmp_path / "p1-26d-harness.js"
    harness_path.write_text(
        r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const sandbox = {
  TMOrderReference:{component:{}},
  axios:{defaults:{},interceptors:{response:{use(){}}}},
  Vue:{createApp(definition){sandbox.definition=definition;return {component(){return this},mount(){return this}}}},
  localStorage:{getItem(){return ""},setItem(){},removeItem(){}},
  window:{},console,URLSearchParams,setTimeout,clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(source,sandbox);
const methods = sandbox.definition.methods;
const calls = [];
const context = {
  ...methods,
  incomingTab:"received",
  requisitionNextStepGuide:{visible:true,documentNo:"BL-UAT",count:1,source:"supplier"},
  hasPermission(permission){return permission === "incoming.execute";},
  pageAllowed(page){return page === "incoming";},
  invalidatePageCache(page){calls.push(`invalidate:${page}`);},
  async go(page){calls.push(`go:${page}`);},
  showToast(message,isError){calls.push(`toast:${message}:${!!isError}`);},
};
(async () => {
  const result = await methods.goToIncomingFromRequisitionGuide.call(context);
  if (!result) throw new Error("Authorized guide did not navigate");
  if (context.incomingTab !== "pending") throw new Error("Guide did not select pending incoming receipts");
  if (context.requisitionNextStepGuide.visible) throw new Error("Guide did not close after navigation");
  if (calls.join("|") !== "invalidate:incoming|go:incoming") throw new Error(calls.join("|"));
  const body = methods.goToIncomingFromRequisitionGuide.toString();
  if (/axios\.|\/api\//.test(body)) throw new Error("Guide action must not write receipt or inventory data");
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

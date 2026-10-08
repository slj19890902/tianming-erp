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


def test_incoming_success_guide_is_short_permission_scoped_and_truthful() -> None:
    guide = _block(
        '<section v-if="incomingNextStepGuide.visible',
        '<section v-if="warehouseFrameUrl || warehouseLedgerUrl"',
    )
    assert "{{ incomingNextStepGuideMessage }}" in guide
    assert "incomingNextStepGuide.hasFinished !== false && canDelivery" in guide
    assert "下一步：去送货" in guide
    assert "继续收料" in guide
    assert "canDelivery && pageAllowed('deliveries')" in guide
    assert "请交给有送货权限的账号继续" in guide
    assert "下一步去生产确认" not in guide


def test_single_receipt_guides_only_order_linked_success() -> None:
    receive = _block("async receiveIncoming(row)", "async acceptShortIncoming(row)")
    write = 'const {data}=await axios.put(`/api/incoming/receive/${row.item_id}`,payload);'
    guide = "this.showIncomingNextStepGuide({"

    assert write in receive
    refresh_after_write = receive.index(
        "const refreshed=await this.refreshIncomingAfterWrite();",
        receive.index(write),
    )
    assert receive.index(write) < receive.index(guide) < refresh_after_write
    assert 'if (row.source_type !== "stock_replenishment")' in receive
    assert 'pendingBalance:data.material_status === "pending"' in receive


def test_batch_receipt_counts_only_successful_order_rows() -> None:
    batch = _block("async batchReceiveIncoming()", "async receiveIncoming(row)")

    assert "const successfulResults = (data.results || []).filter(result => result.success);" in batch
    assert "successfulItemIds.has(String(itemId))" in batch
    assert 'row.source_type !== "stock_replenishment"' in batch
    assert "count:successfulOrderItemIds.size" in batch
    assert 'result.item?.material_status === "pending"' in batch
    write = 'const { data } = await axios.put("/api/incoming/batch-receive",requestPayload);'
    guide = "this.showIncomingNextStepGuide({"
    assert write in batch
    refresh_after_write = batch.index(
        "const refreshed=await this.refreshIncomingAfterWrite();",
        batch.index(write),
    )
    assert batch.index(write) < batch.index(guide) < refresh_after_write


def test_non_receipt_paths_do_not_claim_new_incoming_success() -> None:
    accept_short = _block("async acceptShortIncoming(row)", "async revertIncoming(row)")
    revert = _block("async revertIncoming(row)", "async loadIncomingHistory()")
    payload = _block("incomingPayload(row)", "canReceiveIncoming(row)")

    assert "showIncomingNextStepGuide" not in accept_short
    assert "showIncomingNextStepGuide" not in revert
    assert "showIncomingNextStepGuide" not in payload


def test_incoming_guide_action_only_opens_existing_pending_delivery_page(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend behavior validation"

    scripts = [
        source
        for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if source.strip()
    ]
    assert len(scripts) == 1
    script_path = tmp_path / "p1-26e-index.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    harness_path = tmp_path / "p1-26e-harness.js"
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
  canDelivery:true,
  deliveryDashboardMode:"all",
  incomingNextStepGuide:{visible:true,count:2,pendingBalance:true},
  pageAllowed(page){return page === "deliveries";},
  invalidatePageCache(page){calls.push(`invalidate:${page}`);},
  async go(page){calls.push(`go:${page}`);},
  showToast(message,isError){calls.push(`toast:${message}:${!!isError}`);},
};
(async () => {
  const result = await methods.goToDeliveryFromIncomingGuide.call(context);
  if (!result) throw new Error("Authorized guide did not navigate");
  if (context.deliveryDashboardMode !== "pending_customers") throw new Error("Guide did not select pending deliveries");
  if (context.incomingNextStepGuide.visible) throw new Error("Guide did not close after navigation");
  if (calls.join("|") !== "invalidate:deliveries|go:deliveries") throw new Error(calls.join("|"));
  const body = methods.goToDeliveryFromIncomingGuide.toString();
  if (/axios\.|\/api\//.test(body)) throw new Error("Guide action must not write delivery or inventory data");
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

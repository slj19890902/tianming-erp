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


def test_production_success_guide_is_destination_specific_and_short() -> None:
    guide = _block(
        '<section v-if="productionNextStepGuide.visible',
        '<section v-if="warehouseFrameUrl"',
    )
    assert "已进入一楼待送区，下一步按客户开送货单。" in guide
    assert "继续完成同订单其他组件，整套齐后再开送货单。" in guide
    assert "留在成品仓等待后续送货。" in guide
    assert "下一步：开送货单" in guide
    assert "继续生产" in guide
    assert "productionNextStepGuide.mode === 'direct' && canDelivery && pageAllowed('deliveries')" in guide


def test_direct_completion_shows_guide_only_after_formal_post_succeeds() -> None:
    direct = _block("async confirmProductionDirectRow(row)", "autoSelectProductionRow(row)")
    post = 'await axios.post("/api/production/completion-batches", {'
    guide = "this.showProductionNextStepGuide({"

    assert direct.index(post) < direct.index(guide) < direct.index("delete this.productionDirectAttempts[row.id]")
    assert 'mode:row.is_component_task ? "component" : "direct"' in direct
    assert "return;" in direct[direct.index("catch (error)") : direct.index(guide)]
    assert 'this.productionTab = "history";' in direct


def test_component_completion_never_gets_delivery_primary_action() -> None:
    guide = _block(
        '<section v-if="productionNextStepGuide.visible',
        '<section v-if="warehouseFrameUrl"',
    )
    primary_start = guide.index("下一步：开送货单")
    button_start = guide.rfind("<button", 0, primary_start)
    button = guide[button_start : guide.index("</button>", primary_start)]

    assert "productionNextStepGuide.mode === 'direct'" in button
    assert "component" not in button


def test_stock_batch_guide_counts_only_succeeded_rows_and_keeps_failures() -> None:
    batch = _block("async batchConfirmProduction()", "async transferProductionCompletionToStock(row)")

    assert "const succeededRows = [];" in batch
    assert "const failedGroups = [];" in batch
    assert "succeededRows.push(row);" in batch
    assert "if (succeededRows.length)" in batch
    assert 'mode:"stock"' in batch
    assert "count:succeededRows.length" in batch
    assert "this.inventoryLocation(this.productionLocation(row.location_id))" in batch
    assert "失败项已保留，可直接重试" in batch
    assert batch.index("this.showProductionNextStepGuide({") < batch.index("await Promise.all([")
    assert 'this.productionTab = "history";' in batch


def test_delivery_guide_action_only_opens_pending_customer_view(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend behavior validation"

    scripts = [
        source
        for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if source.strip()
    ]
    assert len(scripts) == 1
    script_path = tmp_path / "p1-26f-index.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    harness_path = tmp_path / "p1-26f-harness.js"
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
const calls = [];
const context = {
  ...methods,
  canDelivery:true,
  pages:{deliveries:9},
  deliveryDashboardMode:"",
  deliveryListFilters:{customer_id:1,keyword:"old"},
  productionNextStepGuide:{visible:true,mode:"direct",count:1,locations:""},
  pageAllowed(page){return page === "deliveries";},
  invalidatePageCache(page){calls.push(`invalidate:${page}`);},
  async go(page){calls.push(`go:${page}`);},
  async loadDeliveryPendingItems(){calls.push("load:pending-deliveries");return true;},
  showToast(message,isError){calls.push(`toast:${message}:${!!isError}`);},
};
(async () => {
  const result = await methods.goToDeliveryFromProductionGuide.call(context);
  if (!result) throw new Error("Authorized guide did not navigate");
  if (context.deliveryDashboardMode !== "pending_customers") throw new Error("Guide did not select pending delivery customers");
  if (context.pages.deliveries !== 1) throw new Error("Guide did not reset delivery page");
  if (context.productionNextStepGuide.visible) throw new Error("Guide did not close after navigation");
  if (calls.join("|") !== "invalidate:deliveries|go:deliveries|load:pending-deliveries") throw new Error(calls.join("|"));
  const body = methods.goToDeliveryFromProductionGuide.toString();
  if (/axios\.|\/api\//.test(body)) throw new Error("Guide action must not create, save, dispatch or print a delivery note");
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

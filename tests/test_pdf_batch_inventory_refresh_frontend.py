from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"


def test_pdf_batch_shares_lots_and_refreshes_versions_before_each_save() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the PDF batch inventory test"

    harness = r"""
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(process.argv[2], "utf8");
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1])
  .find(source => source.trim());
const sandbox = {
  axios: { defaults:{}, interceptors:{ response:{ use() {} } } },
  Vue: { createApp(definition) { sandbox.definition = definition; return { component() { return this; }, mount() { return this; } }; } },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  window: {}, console, URLSearchParams, setTimeout, clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
const methods = sandbox.definition.methods;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
function inventoryState(candidate) {
  const part = () => ({ candidates:[], manual_candidates:[], selected:null, selected_candidates:[], allocations:[], skipped:true, manual_override:false, general_confirmation:false, unavailable_reason:"" });
  const state = {
    loading:false, stale:false, api_error:false, error:"", context:null,
    authoritative:null, authoritative_loading:false, authoritative_error:"",
    finished:part(), semi:{ whole:part(), cover:part(), base:part() },
  };
  state.finished = {
    ...part(), skipped:false, candidates:[candidate], selected:candidate,
    selected_candidates:[candidate],
  };
  return state;
}
function item(name, quantity, version=1) {
  const candidate = { lot_id:44, lot_number:"LOT-44", version, quantity_available:30 };
  return {
    client_line_id:`line-${name}`, matched_product_id:1, is_new_product:false,
    product_name:name, quantity, unit_price:"1.00", material_candidates:[],
    _inventory_product:{ box_style:"" }, _inventory:inventoryState(candidate),
  };
}
function draft(name, line) {
  return {
    source_name:name, confirmed:true, _save_status:"idle", _save_message:"",
    matched_customer_id:1, customer_po:`PO-${name}`, order_date:"2026-08-04",
    delivery_date:null, preview_safety_token:`token-${name}`,
    integrity_check:{ integrity_status:"passed", integrity_errors:[] }, items:[line],
  };
}

const first = draft("first.pdf", item("first", 20));
const second = draft("second.pdf", item("second", 25));
const context = {
  ...methods,
  orderForm:{ items:[] },
  orderImportBatch:{ retryDraft:null },
  orderImportDrafts:[first, second],
  modal:{ type:"orderPdfImport" },
  loading:false,
  schedulePdfDraftInventoryAuthority() {},
  canConfirmImportDraft() { return true; },
  inventoryDecisionRequired() { return ""; },
  refreshPdfPriceConflict() {},
  pdfImportItemMaterialCode() { return "A"; },
  async loadOrders() {}, async loadKpi() {}, showToast() {},
};

methods.reallocateAllDraftInventory.call(context);
assert(first.items[0]._inventory.finished.allocations[0].requested_qty === 20, "first draft allocation changed");
assert(second.items[0]._inventory.finished.allocations[0].requested_qty === 10, "second draft reused the full lot instead of the batch remainder");

const exhaustFirst = draft("exhaust-first.pdf", item("exhaust-first", 30));
const exhaustSecond = draft("exhaust-second.pdf", item("exhaust-second", 10));
context.orderImportDrafts = [exhaustFirst, exhaustSecond];
methods.reallocateAllDraftInventory.call(context);
assert(exhaustSecond.items[0]._inventory.finished.allocations.length === 0, "exhausted lot was allocated twice");
assert(exhaustSecond.items[0]._inventory.finished.skipped === true, "safe exhausted lot did not fall back to production");
assert(methods.inventoryDecisionRequired.call(context, exhaustSecond.items[0]) === "", "safe exhausted lot blocked batch confirmation");

context.orderImportDrafts = [first, second];
methods.reallocateAllDraftInventory.call(context);
let serverVersion = 1;
let serverAvailable = 30;
let refreshCalls = 0;
const requestedQuantities = [];
context.loadOrderLineInventory = async line => {
  refreshCalls += 1;
  const freshCandidate = { lot_id:44, lot_number:"LOT-44", version:serverVersion, quantity_available:serverAvailable };
  line._inventory = inventoryState(freshCandidate);
};
sandbox.axios.post = async (url, payload) => {
  assert(url === "/api/orders", `unexpected URL ${url}`);
  const reservation = payload.items[0].reservation_plan.finished[0];
  const expected = reservation.expected_version;
  if (expected !== serverVersion) {
    throw { response:{ status:409, data:{ detail:"库存已被其他订单预占" } } };
  }
  requestedQuantities.push(reservation.requested_qty);
  serverAvailable -= reservation.requested_qty;
  serverVersion += 1;
  return { data:{ id:serverVersion } };
};

(async () => {
  await methods.saveConfirmedImportDrafts.call(context);
  assert(refreshCalls === 2, `expected two authoritative refreshes, got ${refreshCalls}`);
  assert(first._save_status === "success", `first status ${first._save_status}`);
  assert(second._save_status === "success", `second status ${second._save_status}: ${second._save_message}`);
  process.stdout.write(JSON.stringify({ refreshCalls, serverVersion, requestedQuantities }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""

    result = subprocess.run(
        [node, "-", str(INDEX_PATH)],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "refreshCalls": 2,
        "serverVersion": 3,
        "requestedQuantities": [20, 10],
    }

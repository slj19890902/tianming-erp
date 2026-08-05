from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    body = INDEX[match.end() : match.end() + next_method.start()]
    return re.sub(r"\n\s*},\s*$", "", body)


def _run_node(tmp_path: Path, name: str, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for delivery frontend behavior validation"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_loading_state_never_flashes_false_empty_customer_warning() -> None:
    modal_start = INDEX.index('<div v-else-if="modal.type === \'delivery\'">')
    modal_end = INDEX.index('<div v-else-if="modal.type === \'tianhuaPreimport\'">', modal_start)
    modal = INDEX[modal_start:modal_end]

    assert "客户候选加载失败" in modal
    assert '@click="loadDeliveryCustomerOptions"' in modal
    assert (
        "deliveryForm.source_tab === 'order' && !deliveryCandidateLoading "
        "&& !deliveryCandidateError && !deliveryCustomerOptions.length"
    ) in modal
    opener = _method_body("openDelivery")
    assert "const candidatesLoaded = await this.loadDeliveryCustomerOptions()" in opener
    assert 'if (!candidatesLoaded || this.modal?.type !== "delivery") return' in opener


def test_customer_candidate_loader_ignores_stale_success_and_failure(tmp_path: Path) -> None:
    body = _method_body("loadDeliveryCustomerOptions")
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending = [];
global.axios = {{ get() {{ return new Promise((resolve, reject) => pending.push({{resolve, reject}})); }} }};
const vm = {{
  deliveryCandidateRequestSequence: 0,
  deliveryCandidateLoading: false,
  deliveryCandidateError: "",
  deliveryCustomerCandidates: [],
  errorMessage(error) {{ return error.message || String(error); }},
}};
vm.load = new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const first = vm.load();
  const second = vm.load();
  pending[1].resolve({{data:{{items:[{{customer_id:2}}]}}}});
  await second;
  pending[0].reject(new Error("旧请求失败"));
  await first;
  if (vm.deliveryCustomerCandidates.length !== 1 || vm.deliveryCustomerCandidates[0].customer_id !== 2) throw new Error("stale request overwrote latest customers");
  if (vm.deliveryCandidateError) throw new Error("stale error leaked into current form");
  if (vm.deliveryCandidateLoading) throw new Error("latest request did not release loading state");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "p1-09c-41-customer-race.js", source)


def test_customer_switch_invalidates_old_order_and_inventory_candidate_requests(tmp_path: Path) -> None:
    batch_body = _method_body("loadDeliveryBatchItems")
    unordered_body = _method_body("loadUnorderedFinishedCandidates")
    change_body = _method_body("onDeliveryCustomerChange")
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const requests = [];
global.axios = {{ get(url, config) {{ return new Promise((resolve, reject) => requests.push({{url, config, resolve, reject}})); }} }};
const picker = () => ({{visible:true,loading:false,keyword:"",items:[],selected:{{}},page:1,page_size:12,total:0,total_pages:1,request_token:0,message:"",error:false}});
const vm = {{
  deliveryForm: {{editingId:null,customer_id:1,source_mode:"order",source_tab:"order",lines:[]}},
  deliveryBatchPicker: picker(),
  unorderedFinishedPicker: picker(),
  deliveryBatchRequestSequence: 0,
  unorderedFinishedRequestSequence: 0,
  deliveryKitSummary() {{ return ""; }},
  errorMessage(error) {{ return error.message || String(error); }},
  showToast() {{}},
}};
vm.loadBatch = new AsyncFunction("page", {json.dumps(batch_body, ensure_ascii=False)}).bind(vm);
vm.loadInventory = new AsyncFunction("page", {json.dumps(unordered_body, ensure_ascii=False)}).bind(vm);
vm.changeCustomer = new Function({json.dumps(change_body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const oldBatch = vm.loadBatch(1);
  const oldInventory = vm.loadInventory(1);
  vm.deliveryForm.customer_id = 2;
  vm.changeCustomer();
  vm.deliveryBatchPicker.visible = true;
  vm.unorderedFinishedPicker.visible = true;
  const newBatch = vm.loadBatch(1);
  const newInventory = vm.loadInventory(1);
  requests[2].resolve({{data:{{items:[{{order_item_id:22,product_code:"NEW"}}],page:1,total:1,total_pages:1}}}});
  requests[3].resolve({{data:{{items:[{{inventory_lot_id:44,owner_customer_id:2,product_id:9}}],page:1,total:1,total_pages:1}}}});
  await Promise.all([newBatch, newInventory]);
  requests[0].resolve({{data:{{items:[{{order_item_id:11,product_code:"OLD"}}],page:1,total:1,total_pages:1}}}});
  requests[1].reject(new Error("旧库存请求失败"));
  await Promise.all([oldBatch, oldInventory]);
  if (vm.deliveryBatchPicker.items.length !== 1 || vm.deliveryBatchPicker.items[0].product_code !== "NEW") throw new Error("old order candidates overwrote the selected customer");
  if (vm.unorderedFinishedPicker.items.length !== 1 || vm.unorderedFinishedPicker.items[0].owner_customer_id !== 2) throw new Error("old inventory candidates overwrote the selected customer");
  if (vm.deliveryBatchPicker.error || vm.unorderedFinishedPicker.error) throw new Error("stale request error leaked into the new customer");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "p1-09c-41-customer-switch-race.js", source)


def test_inline_javascript_stays_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-41-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

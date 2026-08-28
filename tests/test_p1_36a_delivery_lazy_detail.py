from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(start: str, end: str) -> str:
    start_index = INDEX.index(start) + len(start)
    body = INDEX[start_index : INDEX.index(end, start_index)]
    return re.sub(r"\n\s*},\s*$", "", body)


def _run_node(tmp_path: Path, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for delivery lazy-detail regression"
    target = tmp_path / "p1-36a-delivery-lazy-detail.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_delivery_list_uses_summary_and_detail_is_loaded_once(tmp_path: Path) -> None:
    load_body = _method_body(
        "async loadDeliveryListDetail(row, {force=false} = {}) {",
        "invalidateDeliveryListDetail(deliveryOrId) {",
    )
    toggle_body = _method_body(
        "async toggleDeliveryListDetail(row) {",
        "async sendVersionedMasterMutation(",
    )
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const requests = [];
global.axios = {{ get(url) {{ return new Promise((resolve, reject) => requests.push({{url,resolve,reject}})); }} }};
const vm = {{
  deliveries:[], expandedDeliveryRows:{{}}, deliveryDetailState:{{}}, deliveryDetailRequestSequence:0,
  errorMessage(error) {{ return error?.message || String(error); }},
}};
const loadBody = {json.dumps(load_body, ensure_ascii=False)};
const toggleBody = {json.dumps(toggle_body, ensure_ascii=False)};
vm.loadDeliveryListDetail = new AsyncFunction("row", "opts", `const {{force=false}} = opts || {{}};\n${{loadBody}}`).bind(vm);
vm.toggleDeliveryListDetail = new AsyncFunction("row", toggleBody).bind(vm);
(async () => {{
  const firstRow = {{id:11,item_count:2,delivery_number:"TH-11"}};
  vm.deliveries = [firstRow];
  const firstOpen = vm.toggleDeliveryListDetail(firstRow);
  if (!vm.expandedDeliveryRows[11]) throw new Error("row did not open immediately");
  if (requests.length !== 1 || requests[0].url !== "/api/deliveries/11") throw new Error("detail request missing");
  requests[0].resolve({{data:{{id:11,items:[{{id:1}},{{id:2}}],total_actual_goods_quantity:9}}}});
  if (await firstOpen !== true || !firstRow.detail_loaded || firstRow.items.length !== 2) throw new Error("detail was not cached");

  await vm.toggleDeliveryListDetail(firstRow);
  if (vm.expandedDeliveryRows[11]) throw new Error("row did not close");
  await vm.toggleDeliveryListDetail(firstRow);
  if (!vm.expandedDeliveryRows[11] || requests.length !== 1) throw new Error("cached detail was requested again");

  const secondRow = {{id:22,item_count:1,delivery_number:"TH-22"}};
  vm.deliveries.push(secondRow);
  const failed = vm.toggleDeliveryListDetail(secondRow);
  requests[1].reject(new Error("网络断开"));
  if (await failed !== false) throw new Error("failed detail did not report false");
  if (vm.deliveries.length !== 2 || !vm.deliveryDetailState[22].error.includes("网络断开")) throw new Error("failure cleared list or hid message");
  const retry = vm.loadDeliveryListDetail(secondRow, {{force:true}});
  requests[2].resolve({{data:{{id:22,items:[{{id:3}}]}}}});
  if (await retry !== true || !secondRow.detail_loaded || vm.deliveryDetailState[22].error) throw new Error("retry did not recover");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, source)


def test_summary_row_can_open_editor_only_after_detail_load(tmp_path: Path) -> None:
    edit_body = _method_body(
        "async editDelivery(row) {",
        "async deleteDelivery(row) {",
    )
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const toasts = [];
const vm = {{
  deliverySaveState:{{}}, pendingDeliveryItems:[], deliveryDetailState:{{}}, modal:null,
  deliveryBatchPicker:{{}}, unorderedFinishedPicker:{{}},
  deliverySourceModeFromLines() {{ return "order_only"; }},
  createDeliveryLine(value) {{ return {{...value}}; }},
  deliveryFormSignature() {{ return "saved"; }},
  resetDeliveryReminderState() {{}},
  loadDeliveryRemindersForCustomer: async () => true,
  loadDeliveryPendingItems: async () => true,
  showToast(message, error) {{ toasts.push({{message,error}}); }},
}};
vm.editDelivery = new AsyncFunction("row", {json.dumps(edit_body, ensure_ascii=False)}).bind(vm);
(async () => {{
  let loads = 0;
  vm.loadDeliveryListDetail = async row => {{
    loads += 1;
    row.detail_loaded = true;
    row.items = [{{id:1,source_type:"order",order_item_id:9,order_id:8,delivered_quantity:5}}];
    return true;
  }};
  const row = {{id:11,delivery_number:"TH-11",customer_id:5,delivery_date:"2026-08-09"}};
  if (await vm.editDelivery(row) !== true) throw new Error("summary row did not open editor");
  if (loads !== 1 || vm.deliveryForm.lines.length !== 1 || vm.modal?.type !== "delivery") throw new Error("editor opened without one loaded line");

  vm.modal = null;
  vm.loadDeliveryListDetail = async row => {{
    loads += 1;
    vm.deliveryDetailState[row.id] = {{error:"连接失败"}};
    return false;
  }};
  const failedRow = {{id:22,delivery_number:"TH-22"}};
  if (await vm.editDelivery(failedRow) !== false) throw new Error("failed detail unexpectedly opened editor");
  if (vm.modal !== null || !toasts.at(-1)?.message.includes("连接失败")) throw new Error("failed detail did not preserve list and explain error");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, source)


def test_detail_request_tokens_do_not_collide_after_list_refresh(tmp_path: Path) -> None:
    load_body = _method_body(
        "async loadDeliveryListDetail(row, {force=false} = {}) {",
        "invalidateDeliveryListDetail(deliveryOrId) {",
    )
    invalidate_body = _method_body(
        "invalidateDeliveryListDetail(deliveryOrId) {",
        "async toggleDeliveryListDetail(row) {",
    )
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor = Object.getPrototypeOf(function(){{}}).constructor;
const requests = [];
global.axios = {{ get(url) {{ return new Promise((resolve, reject) => requests.push({{url,resolve,reject}})); }} }};
const vm = {{
  deliveries:[], expandedDeliveryRows:{{}}, deliveryDetailState:{{}}, deliveryDetailRequestSequence:0,
  errorMessage(error) {{ return error?.message || String(error); }},
}};
const loadBody = {json.dumps(load_body, ensure_ascii=False)};
vm.loadDeliveryListDetail = new AsyncFunction("row", "opts", `const {{force=false}} = opts || {{}};\n${{loadBody}}`).bind(vm);
vm.invalidateDeliveryListDetail = new FunctionCtor("deliveryOrId", {json.dumps(invalidate_body, ensure_ascii=False)}).bind(vm);
(async () => {{
  const oldRow = {{id:11}};
  vm.deliveries = [oldRow];
  const oldRequest = vm.loadDeliveryListDetail(oldRow);
  vm.deliveryDetailState = {{}};
  const newRow = {{id:11}};
  vm.deliveries = [newRow];
  const newRequest = vm.loadDeliveryListDetail(newRow);
  if (requests.length !== 2) throw new Error("refresh did not permit a new detail request");
  requests[0].resolve({{data:{{id:11,items:[{{id:"old"}}]}}}});
  if (await oldRequest !== false || oldRow.detail_loaded || newRow.items) throw new Error("stale response crossed the refresh boundary");
  requests[1].resolve({{data:{{id:11,items:[{{id:"new"}}]}}}});
  if (await newRequest !== true || newRow.items[0].id !== "new") throw new Error("latest detail response was not retained");

  vm.invalidateDeliveryListDetail(newRow);
  if (newRow.items || newRow.detail_loaded || vm.expandedDeliveryRows[11] !== false) throw new Error("write invalidation left stale detail visible");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, source)


def test_delivery_first_paint_contract_is_visible_and_lightweight() -> None:
    loader = _method_body("async loadDeliveries() {", "async loadDeliveryPendingItems() {")
    assert 'view:"summary"' in loader
    assert "this.expandedDeliveryRows = {}" in loader
    assert "this.deliveryDetailState = {}" in loader
    assert "row.item_count ?? (row.items || []).length" in INDEX
    assert "正在读取这张送货单的明细" in INDEX
    assert "明细读取失败" in INDEX
    assert "loadDeliveryListDetail(row,{force:true})" in INDEX
    assert "this.invalidateDeliveryListDetail(Number(data.id))" in INDEX
    assert "this.invalidateDeliveryListDetail(Number(data.delivery_id))" in INDEX
    assert "this.invalidateDeliveryListDetail(deliveryId)" in INDEX
    assert "this.invalidateDeliveryListDetail(row)" in INDEX

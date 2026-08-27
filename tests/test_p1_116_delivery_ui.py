from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


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
    assert node, "Node.js is required for delivery UI behavior validation"
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


def test_receipt_primary_actions_have_distinct_fact_based_colors(tmp_path: Path) -> None:
    deliveries = _between(
        INDEX,
        '<template v-else-if="activePage === \'deliveries\'">',
        '<template v-else-if="activePage === \'finance\'">',
    )
    assert ':class="deliveryPrimaryRowActionClass(row)"' in deliveries
    assert ".delivery-receipt-edit-action" in INDEX

    body = _method_body("deliveryPrimaryRowActionClass")
    source = f"""
const Fn=Object.getPrototypeOf(function(){{}}).constructor;
const classify=new Fn("row",{json.dumps(body, ensure_ascii=False)});
const vm={{canFinance:true}};
if(classify.call(vm,{{status:"dispatched",return_receipt_status:"waiting_receipt"}})!=="success")throw new Error("unconfirmed receipt color");
if(classify.call(vm,{{status:"dispatched",return_receipt_status:"confirmed"}})!=="delivery-receipt-edit-action")throw new Error("confirmed receipt color");
if(classify.call(vm,{{status:"pending",return_receipt_status:null}})!=="")throw new Error("non receipt action color changed");
"""
    _run_node(tmp_path, "p1-116-receipt-colors.js", source)


def test_new_delivery_defaults_to_order_customers_and_filters_exact_source_union(
    tmp_path: Path,
) -> None:
    modal = _between(
        INDEX,
        '<div v-else-if="modal.type === \'delivery\'">',
        '<div v-else-if="modal.type === \'tianhuaPreimport\'">',
    )
    assert 'v-model="deliveryCustomerSources.orders"' in modal
    assert 'v-model="deliveryCustomerSources.inventory"' in modal
    assert "有订单客户" in modal
    assert "有库存客户" in modal
    assert '@change="onDeliveryCustomerSourceFilterChange"' in modal

    opener = _method_body("openDelivery")
    assert "this.deliveryCustomerSources = {orders:true, inventory:false}" in opener

    options = _method_body("deliveryCustomerOptions")
    source = f"""
const Fn=Object.getPrototypeOf(function(){{}}).constructor;
const options=new Fn({json.dumps(options, ensure_ascii=False)});
const vm={{
  customerOptions:[
    {{id:1,is_active:true,name:"仅订单"}},
    {{id:2,is_active:true,name:"仅库存"}},
    {{id:3,is_active:true,name:"两者都有"}},
    {{id:4,is_active:true,name:"无来源"}},
  ],
  deliveryCustomerCandidates:[
    {{customer_id:1,has_pending_orders:true,pending_item_count:1,has_unordered_finished:false}},
    {{customer_id:2,has_pending_orders:false,has_unordered_finished:true,unordered_lot_count:2}},
    {{customer_id:3,has_pending_orders:true,pending_item_count:3,has_unordered_finished:true,unordered_lot_count:4}},
  ],
  deliveryCustomerSources:{{orders:true,inventory:false}},
  deliveryForm:{{editingId:null,customer_id:null}},
}};
const ids=()=>options.call(vm).map(row=>row.id).join(",");
if(ids()!=="1,3")throw new Error(`order source ${{ids()}}`);
vm.deliveryCustomerSources={{orders:false,inventory:true}};
if(ids()!=="2,3")throw new Error(`inventory source ${{ids()}}`);
vm.deliveryCustomerSources={{orders:false,inventory:false}};
if(ids()!=="")throw new Error(`empty sources ${{ids()}}`);
vm.deliveryForm={{editingId:88,customer_id:2}};
if(ids()!=="2")throw new Error(`editing customer disappeared ${{ids()}}`);
"""
    _run_node(tmp_path, "p1-116-customer-source-union.js", source)


def test_customer_source_change_clears_an_ineligible_new_delivery_selection(
    tmp_path: Path,
) -> None:
    body = _method_body("onDeliveryCustomerSourceFilterChange")
    source = f"""
const Fn=Object.getPrototypeOf(function(){{}}).constructor;
const change=new Fn({json.dumps(body, ensure_ascii=False)});
let changed=0;
const vm={{
  deliveryForm:{{editingId:null,customer_id:2}},
  deliveryCustomerOptions:[{{id:1}}],
  onDeliveryCustomerChange(){{changed+=1;}},
}};
change.call(vm);
if(vm.deliveryForm.customer_id!==null||changed!==1)throw new Error("ineligible customer was not cleared");
vm.deliveryForm={{editingId:7,customer_id:2}};
change.call(vm);
if(vm.deliveryForm.customer_id!==2||changed!==1)throw new Error("editing customer was changed");
"""
    _run_node(tmp_path, "p1-116-customer-source-reset.js", source)


def test_vehicle_field_moves_below_customer_source_filters() -> None:
    modal = _between(
        INDEX,
        '<div v-else-if="modal.type === \'delivery\'">',
        '<div v-else-if="modal.type === \'tianhuaPreimport\'">',
    )
    header = _between(modal, '<div class="form-grid delivery-header-grid">', "</div>\n              </div>")
    assert "delivery-customer-source-field" in header
    assert 'v-model.trim="deliveryForm.vehicle_number"' not in header
    source_toolbar = _between(
        modal,
        '<div class="delivery-source-toolbar"',
        '<div v-if="deliveryForm.source_tab === \'unordered_finished\'"',
    )
    assert 'v-model.trim="deliveryForm.vehicle_number"' in source_toolbar
    assert source_toolbar.index("车号") < source_toolbar.index("送货来源")


def test_pending_quantity_column_is_six_digits_wide_and_order_location_expands() -> None:
    modal = _between(
        INDEX,
        '<div v-else-if="modal.type === \'delivery\'">',
        '<div v-else-if="modal.type === \'tianhuaPreimport\'">',
    )
    table = _between(modal, '<table v-else class="delivery-batch-table">', "</table>")
    assert 'class="delivery-batch-order-location-col"' in table
    assert 'class="delivery-batch-remaining-col"' in table
    assert 'class="delivery-batch-remaining-cell"' in table
    assert re.search(
        r"\.delivery-batch-table \.delivery-batch-remaining-col\s*\{[^}]*width:\s*(?:7[0-9]|8[0-4])px",
        INDEX,
    )
    assert re.search(
        r"\.delivery-batch-table \.delivery-batch-order-location-col\s*\{[^}]*width:\s*auto",
        INDEX,
    )


def test_index_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-116-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

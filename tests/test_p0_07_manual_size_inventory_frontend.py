from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
TIME_UTILS = (ROOT / "static" / "assets" / "time-utils.js").read_text(
    encoding="utf-8"
)


def _inline_script() -> str:
    return next(
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL
        )
        if script.strip()
    )


def test_manual_size_row_hides_inventory_controls() -> None:
    order_start = INDEX.index('<div v-else-if="modal.type === \'order\'">')
    order_end = INDEX.index('<div v-else-if="modal.type === \'orderEdit\'">', order_start)
    order_template = INDEX[order_start:order_end]
    assert (
        'v-if="inventoryPlanApplies(item) && item._inventory" '
        ":class=\"['inventory-auto-summary'"
    ) in order_template
    assert order_template.count(
        'v-if="inventoryPlanApplies(item) && item._inventory"'
    ) >= 2


def test_manual_size_inventory_gate_and_existing_product_gate_are_separate() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the frontend behavior test"
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
vm.runInContext({json.dumps(_inline_script())}, sandbox);
const methods = sandbox.definition.methods;
const state = methods.newOrderInventoryState();
state.stale = true;
const manual = {{
  manual_size_entry:true, product_id:null, matched_product_id:null,
  product_name:"匿名纸箱", box_type:"A1", length_mm:520, width_mm:350, height_mm:300,
  material_id:1, flute_type:"AB", quantity:200, unit_price:"3.68", _quote_preference_id:11,
  _inventory:state, bom_component_demands:[],
}};
const formal = {{
  manual_size_entry:false, product_id:9, product_name:"已有常用箱",
  specification:"520×350×300mm", quantity:200, unit_price:"3.68",
  _inventory:{{...state}}, bom_component_demands:[],
}};
const context = {{
  orderForm:{{customer_id:1,items:[manual]}},
  inventoryPlanApplies:methods.inventoryPlanApplies,
  inventoryDecisionRequired:methods.inventoryDecisionRequired,
  inventoryStateMatchesLine:() => true,
  inventoryComponents:() => ["whole"],
  componentLabel:methods.componentLabel,
  isGeneralSemiFinishedCandidate:() => false,
  newOrderInventoryState:methods.newOrderInventoryState,
  manualSizePreferenceOptions:[{{id:11,material_id:1}}],
}};
const manualGate = methods.inventoryDecisionRequired.call(context, manual);
const manualValidation = methods.validateOrderForm.call(context);
const manualPlan = methods.buildReservationPlan.call(context, manual);
context.orderForm.items = [formal];
const formalGate = methods.inventoryDecisionRequired.call(context, formal);
const formalValidation = methods.validateOrderForm.call(context);
if (manualGate !== "" || manualValidation !== "") throw new Error(`manual blocked: ${{manualGate}} / ${{manualValidation}}`);
if (formalGate !== "库存候选已过期，请重新获取并确认。" || !formalValidation.includes("库存候选已过期")) throw new Error("formal stale gate was relaxed");
if (JSON.stringify(manualPlan) !== JSON.stringify({{finished:[],semi:[]}})) throw new Error("manual reservation plan must stay empty");
"""
    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_manual_size_quantity_change_does_not_create_stale_inventory_state() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the frontend behavior test"
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
vm.runInContext({json.dumps(_inline_script())}, sandbox);
const methods = sandbox.definition.methods;
const manual = {{manual_size_entry:true,product_id:null,matched_product_id:null,quantity:200,_inventory:null,_inventory_refresh_timer:null}};
const formal = {{manual_size_entry:false,product_id:9,quantity:200,_inventory:methods.newOrderInventoryState(),_inventory_refresh_timer:null}};
const context = {{
  orderForm:{{customer_id:1}},
  inventoryPlanApplies:methods.inventoryPlanApplies,
  invalidateLineInventory:methods.invalidateLineInventory,
  scheduleOrderLineInventoryRefresh:methods.scheduleOrderLineInventoryRefresh,
  newOrderInventoryState:methods.newOrderInventoryState,
  loadOrderLineInventory() {{}},
  syncOrderBomDemandsForQuantity() {{}},
}};
methods.onOrderDraftQuantityInput.call(context, manual);
if (manual._inventory !== null || manual._inventory_refresh_timer) throw new Error("manual quantity created inventory state");
methods.onOrderDraftQuantityInput.call(context, formal);
if (!formal._inventory.stale || !formal._inventory_refresh_timer) throw new Error("formal quantity did not invalidate inventory");
clearTimeout(formal._inventory_refresh_timer);
const reused = {{
  product_id:null,matched_product_id:null,is_new_product:false,product_code:"",product_name:"",
  quantity:null,unit_price:"",_inventory:methods.newOrderInventoryState(),
  _inventory_product:{{id:99}},_show_inventory_details:true,
  _inventory_refresh_timer:setTimeout(() => {{}},10000),
}};
reused._inventory.stale = true;
const addContext = {{
  orderForm:{{items:[reused]}},
  orderLineIsBlank:() => true,
  manualSizeBoxTypeOptions:["A1"],
  showToast() {{}},
  refreshOrderNumberPreview() {{}},
}};
methods.addManualSizeOrderItem.call(addContext);
if (reused._inventory !== null || reused._inventory_product !== null || reused._show_inventory_details || reused._inventory_refresh_timer) throw new Error("reused blank line kept inventory state");
const payload = methods.orderFormItemPayload.call(
  {{inventoryPlanApplies:methods.inventoryPlanApplies,buildReservationPlan:methods.buildReservationPlan}},
  reused
);
if (payload.reservation_plan !== null) throw new Error("manual payload carried reservation plan");
"""
    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_estimated_price_source_is_translated_without_repricing() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the frontend behavior test"
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
vm.runInContext({json.dumps(_inline_script())}, sandbox);
const methods = sandbox.definition.methods;
const text = methods.manualSizeQuoteText.call(
  {{money:value => Number(value).toFixed(2)}},
  {{_quote_preview:{{final_price_source:"estimated",customer_square_price:"2.50",estimated_unit_price:"3.68"}}}}
);
if (!text.startsWith("系统估价｜") || text.includes("estimated")) throw new Error(text);
if (!text.includes("￥3.68")) throw new Error("estimated price changed");
"""
    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr

from pathlib import Path
import shutil
import subprocess


INDEX = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
START = INDEX.index("saveStockPolicyFromLine(line) {")
METHODS = INDEX[START:INDEX.index("async loadProduction()", START)].strip().rstrip(",")


def run_node(body: str) -> None:
    node = shutil.which("node")
    assert node, "Node is required for the stock policy form behavior tests"
    script = r"""
const assert = require('node:assert/strict');
const posts = [], gets = [];
const axios = {
  post: async (url, payload) => { posts.push({url, payload}); return {data:{id:7}}; },
  get: async (url, config) => { gets.push({url, config}); return {data:{items:[{id:7}]}}; },
};
const line = {_key:'line-1', target_inventory_type:'semi_finished', product_name:'Board A',
  reference_product_id:19, customer_id:4, material_code:'BC', layer_count:5, flute_type:'AB',
  report_length_mm:600, report_width_mm:400, sheet_type:'raw_board', component_type:'whole',
  pieces_per_box:1, stock_yield_per_sheet:2, quantity:25, remark:'keep this remark'};
const ui = {
  canRequisition:true, stockPolicyForms:{}, stockPolicyWarnings:[], messages:[], validation:'',
  modal:{type:'stockReplenishment'}, validationCalls:0,
  stockReplenishmentForm:{supplier_name:'Paper Mill', items:[line], remark:'unsaved draft'},
  validateStockReplenishmentForm() { this.validationCalls++; return this.validation; },
  showToast(message, error=false) { this.messages.push({message,error}); },
  errorMessage: error => error.message,
  METHODS
};
(async () => { BODY })().catch(error => { console.error(error); process.exitCode=1; });
""".replace("METHODS", METHODS).replace("BODY", body)
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_inline_editor_defaults_and_cancel_preserve_replenishment_draft() -> None:
    run_node(r"""
const before = JSON.stringify({modal:ui.modal, draft:ui.stockReplenishmentForm});
assert.equal(ui.saveStockPolicyFromLine(line), true);
const form = ui.stockPolicyForms[line._key];
assert.equal(form.policy_name, 'Board A');
assert.equal(form.warning_quantity, 10);
assert.equal(form.target_quantity, 25);
form.warning_quantity = null;
assert.equal(ui.cancelStockPolicyFromLine(line), true);
assert.equal(await ui.submitStockPolicyFromLine(line), false);
assert.equal(posts.length, 0); assert.equal(gets.length, 0);
assert.equal(JSON.stringify({modal:ui.modal, draft:ui.stockReplenishmentForm}), before);
""")


def test_empty_and_invalid_inputs_never_post_and_existing_guards_remain() -> None:
    run_node(r"""
ui.canRequisition = false;
assert.equal(ui.saveStockPolicyFromLine(line), false);
ui.canRequisition = true; ui.validation = 'other draft line is incomplete';
assert.equal(ui.saveStockPolicyFromLine(line), false);
ui.validation = ''; ui.saveStockPolicyFromLine(line);
const form = ui.stockPolicyForms[line._key];
for (const values of [
  ['', 0, 20], ['   ', 0, 20], ['x'.repeat(201), 0, 20],
  ['valid', null, 20], ['valid', '', 20], ['valid', ' ', 20],
  ['valid', 0, null], ['valid', 0, ''], ['valid', 0, 0],
  ['valid', -1, 20], ['valid', 1.5, 20], ['valid', 'no', 20],
  ['valid', 10, 9], ['valid', 0, 2.5], ['valid', 0, Infinity],
]) {
  [form.policy_name, form.warning_quantity, form.target_quantity] = values;
  assert.equal(await ui.submitStockPolicyFromLine(line), false, JSON.stringify(values));
}
Object.assign(form, {policy_name:'valid', warning_quantity:0, target_quantity:20});
ui.canRequisition = false;
assert.equal(await ui.submitStockPolicyFromLine(line), false);
ui.canRequisition = true; ui.validation = 'draft changed while editing';
assert.equal(await ui.submitStockPolicyFromLine(line), false);
assert.equal(posts.length, 0); assert.equal(gets.length, 0);
assert.equal(form.saving, false);
""")


def test_valid_form_posts_legacy_payload_once_and_prevents_double_click() -> None:
    run_node(r"""
let release;
axios.post = (url, payload) => {
  posts.push({url, payload}); return new Promise(resolve => { release = resolve; });
};
ui.saveStockPolicyFromLine(line);
const form = ui.stockPolicyForms[line._key];
Object.assign(form, {policy_name:'  Board reminder  ', warning_quantity:'0', target_quantity:'25'});
const saving = ui.submitStockPolicyFromLine(line);
assert.equal(form.saving, true);
assert.equal(await ui.submitStockPolicyFromLine(line), false);
assert.equal(ui.saveStockPolicyFromLine(line), false);
assert.equal(ui.cancelStockPolicyFromLine(line), false);
release({data:{id:7}});
assert.equal(await saving, true);
assert.deepEqual(posts, [{url:'/api/requisition/stock-policies', payload:{
  policy_name:'Board reminder', target_inventory_type:'semi_finished', product_id:19,
  customer_id:4, material_code:'BC', layer_count:5, flute_type:'AB', report_length_mm:600,
  report_width_mm:400, sheet_type:'raw_board', component_type:'whole', pieces_per_box:1,
  stock_yield_per_sheet:2, warning_quantity:0, target_quantity:25, default_location_id:null,
  supplier_name:'Paper Mill', remark:'keep this remark', active:true,
}}]);
assert.equal(gets.length, 1);
assert.deepEqual(ui.stockPolicyWarnings, [{id:7}]);
assert.equal(form.committed, true); assert.equal(form.saving, false);
assert.equal(await ui.submitStockPolicyFromLine(line), false);
assert.equal(posts.length, 1);
""")


def test_successful_write_with_failed_refresh_cannot_create_a_second_policy() -> None:
    run_node(r"""
ui.stockPolicyWarnings = [{id:3}];
axios.get = async () => { throw new Error('refresh offline'); };
ui.saveStockPolicyFromLine(line);
assert.equal(await ui.submitStockPolicyFromLine(line), true);
const form = ui.stockPolicyForms[line._key];
assert.equal(form.committed, true); assert.equal(form.saving, false);
assert.equal(form.refreshFailed, true);
assert.deepEqual(ui.stockPolicyWarnings, [{id:3}]);
assert.match(ui.messages.at(-1).message, /已保存.*刷新失败.*无需重复保存/);
assert.equal(ui.messages.at(-1).error, true);
assert.equal(await ui.submitStockPolicyFromLine(line), false);
assert.equal(ui.cancelStockPolicyFromLine(line), true);
assert.equal(ui.saveStockPolicyFromLine(line), false);
assert.equal(posts.length, 1);
""")


def test_rejected_write_keeps_editor_values_and_allows_a_corrected_submission() -> None:
    run_node(r"""
ui.saveStockPolicyFromLine(line);
const form = ui.stockPolicyForms[line._key];
form.policy_name = 'my reminder';
axios.post = async () => { throw new Error('server validation rejected'); };
assert.equal(await ui.submitStockPolicyFromLine(line), false);
assert.equal(form.committed, false); assert.equal(form.saving, false);
assert.equal(form.open, true); assert.equal(form.policy_name, 'my reminder');
assert.equal(gets.length, 0);
axios.post = async (url, payload) => { posts.push({url,payload}); return {data:{id:7}}; };
assert.equal(await ui.submitStockPolicyFromLine(line), true);
assert.equal(posts.length, 1);
""")

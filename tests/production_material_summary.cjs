// Isolated formatting contract; this is a read-only presentation helper.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let mixin;
global.window = global;
global.document = {activeElement: null};
eval(fs.readFileSync(path.resolve(__dirname, '../static/ui/production-desk.js'), 'utf8'));
global.ERPProductionDesk.install({mixin(value) { mixin = value; }});
const vm = {};
for (const [name, method] of Object.entries(mixin.methods)) vm[name] = method.bind(vm);

const sources = [
  {reservation_id: 1, remaining_sheet_quantity: 3, unit: '张'},
  {reservation_id: 2, remaining_sheet_quantity: 2, unit: '张'},
  {reservation_id: 3, remaining_sheet_quantity: 7, unit: '片'},
];
const row = {customer_board_preparation_sources: sources};
const before = JSON.stringify(row);
assert.equal(vm.materialSourcePlanSummary(row), '5 张 / 7 片');
assert.equal(vm.materialSourcePlanSummary({customer_board_preparation_sources: [{remaining_sheet_quantity: 'bad', unit: '张'}]}), '数量待核');
assert.equal(JSON.stringify(row), before, 'summary must not modify reservations or quantities');
const page = fs.readFileSync(path.resolve(__dirname, '../static/index.html'), 'utf8');
assert.match(page, /合计计划取用 \{\{materialSourcePlanSummary\(row\)\}\}/);
console.log('multi-source production summary separates units and remains read-only');

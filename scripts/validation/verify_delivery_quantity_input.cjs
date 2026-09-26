const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
  if (!/\bsrc=/.test(match[1]) && !/application\/json/.test(match[1])) new vm.Script(match[2]);
}
function method(name, next) {
  const start = html.indexOf('          ' + name + '(');
  const end = html.indexOf('          ' + next + '(', start);
  assert(start > 0 && end > start);
  return vm.runInNewContext('({' + html.slice(start, end) + '})')[name];
}
const context = {
  deliveryForm: {lines: []},
  unorderedFinishedPicker: {selected: {}},
  showToast(text, error) { if (error) throw Error(text); },
  createDeliveryLine(value) { return value; },
};
for (const [name, next] of [
  ['unorderedFinishedLotId', 'unorderedFinishedAvailableQuantity'],
  ['unorderedFinishedAvailableQuantity', 'unorderedFinishedPhysicalQuantity'],
  ['unorderedFinishedPhysicalQuantity', 'syncUnorderedDeliveryLineQuantity'],
  ['syncUnorderedDeliveryLineQuantity', 'isUnorderedFinishedLotInForm'],
  ['isUnorderedFinishedLotInForm', 'async toggleUnorderedFinishedPicker'],
  ['importUnorderedFinishedCandidates', 'deliveryLineHasInput'],
]) context[name] = method(name, next);
const basis = {customer_basis: 1, physical_basis: 2, customer_unit: '只', physical_unit: '片'};
function add(lot, product, customerQuantity, contract) {
  context.unorderedFinishedPicker.selected[lot] = {
    inventory_lot_id: lot, product_id: product, _selected: true, _quantity: customerQuantity,
    available_quantity: 200, available_customer_quantity: contract ? 100 : 200,
    quantity_contract: contract,
  };
  assert.equal(context.importUnorderedFinishedCandidates(), true);
}
add(1, 1, 100, basis);
assert.equal(context.deliveryForm.lines[0].delivered_quantity, 100);
assert.equal(context.deliveryForm.lines[0].allocations[0].quantity, 200);
add(2, 2, 50, basis);
assert.equal(context.deliveryForm.lines[1].allocations[0].quantity, 100);
add(3, 2, 50, basis);
assert.equal(context.deliveryForm.lines[1].delivered_quantity, 100);
assert.equal(context.deliveryForm.lines[1].allocations.reduce((s, x) => s + x.quantity, 0), 200);
add(4, 3, 50, null);
assert.equal(context.deliveryForm.lines[2].allocations[0].quantity, 50);
const reopened = JSON.parse(JSON.stringify(context.deliveryForm.lines[0]));
assert.equal(context.syncUnorderedDeliveryLineQuantity(reopened), 100);
assert.equal(context.syncUnorderedDeliveryLineQuantity(reopened), 100);
assert.equal(context.unorderedFinishedAvailableQuantity({available_quantity: 199, available_customer_quantity: 99}), 99);
console.log('PASS: input100/physical200, input50/physical100, split lots, mixed1:1, reopen no repeat, odd stock; inline script syntax');

const planner = {
  inventoryComponents() {return [];}, inventoryPlanApplies() {return true;},
  isSafeSystemInventoryCandidate() {return true;},
  finishedInventoryAllocatedCustomerQuantity: method('finishedInventoryAllocatedCustomerQuantity','reallocateDraftInventorySequentially'),
  reallocateDraftInventorySequentially: method('reallocateDraftInventorySequentially','schedulePdfDraftInventoryAuthority'),
  buildReservationPlan: method('buildReservationPlan','inventoryCandidateScope'),
};
function draft(quantity, stocks, customerBasis=1, physicalBasis=2) {
  const candidates=stocks.map((stock,i)=>({lot_id:i+1,version:1,quantity_available:stock,
    quantity_contract:{customer_basis:customerBasis,physical_basis:physicalBasis}}));
  return {quantity,_inventory:{finished:{candidates,manual_candidates:[],selected_candidates:candidates},semi:{}}};
}
for (const [stocks,requested,expected,physical] of [[[200],100,100,200],[[199],100,99,198],[[1,1],1,1,2],[[1,2],1,1,2]]) {
  const line=draft(requested,stocks);
  planner.reallocateDraftInventorySequentially([line]);
  assert.equal(planner.finishedInventoryAllocatedCustomerQuantity(line._inventory.finished),expected);
  assert.equal(line._inventory.finished.allocations.reduce((sum,row)=>sum+row.stock_quantity,0),physical);
  assert(planner.buildReservationPlan(line).finished.every(row=>Number.isInteger(row.requested_qty)));
}
const shared=[draft(1,[1,1]),draft(1,[1,1])];
planner.reallocateDraftInventorySequentially(shared);
assert.deepEqual(shared.map(line=>planner.finishedInventoryAllocatedCustomerQuantity(line._inventory.finished)),[1,0]);
const twoForThree=draft(2,[1,2],2,3);
planner.reallocateDraftInventorySequentially([twoForThree]);
assert.equal(planner.finishedInventoryAllocatedCustomerQuantity(twoForThree._inventory.finished),2);
assert.deepEqual(Array.from(planner.buildReservationPlan(twoForThree).finished,row=>row.requested_qty),[2,2]);
console.log('PASS: new/import order planner preserves physical stock, cross-lot fractions, shared stock and whole request groups');
const pdfSandbox={};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/ui/pdf-workspace.js'),'utf8'),pdfSandbox);
let pdfMixin;
pdfSandbox.ERPPdfWorkspace.install({mixin(value){pdfMixin=value;}});
assert.equal(pdfMixin.methods.pdfFinishedUse({_inventory:{finished:{allocations:[
  {candidate:{lot_id:1},requested_qty:100,stock_quantity:200}
]}}},{lot_id:1}),200);
console.log('PASS: PDF inventory used/available comparison uses the same physical unit');

(async () => {
  const start = html.indexOf('          async loadFinishedInventoryCandidates(');
  const end = html.indexOf('          async loadFinishedInventoryReservations(', start);
  assert(start > 0 && end > start);
  const load = vm.runInNewContext('({' + html.slice(start, end) + '})', {
    axios: {get: async () => ({data: {remaining_requirement: 200, items: [
      {lot_id: 1, quantity_available: 200, customer_quantity_available: 100},
      {lot_id: 2, quantity_available: 199, customer_quantity_available: 99},
      {lot_id: 3, quantity_available: 50, customer_quantity_available: 50},
      {lot_id: 4, quantity_available: 1, customer_quantity_available: 0},
    ]}})},
  }).loadFinishedInventoryCandidates;
  const form = {id: 1};
  const state = {orderItemForm: form, modal: {type: 'orderItem'},
    showToast(message) {throw Error(message);}, errorMessage(error) {return String(error);}};
  assert.equal(await load.call(state), true);
  assert.deepEqual(Array.from(state.finishedInventoryCandidates, row => row._reserve_qty), [100, 99, 50, 0]);
  console.log('PASS: finished stock reservation defaults use customer capacity; odd physical piece is not rounded up');
})().catch(error => {console.error(error); process.exitCode = 1;});

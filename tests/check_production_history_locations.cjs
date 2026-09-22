const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
function method(name) {
  const match = html.match(new RegExp(`          ${name}\\(row\\) \\{([\\s\\S]*?)\\n          \\},`));
  assert(match, name);
  return vm.runInNewContext(`(function(row) {${match[1]}\n})`, {URLSearchParams, window});
}
let opened;
const window = {open: url => { opened = url; }};
const context = {showProductionMap: row => { opened = '/warehouse.html?' + new URLSearchParams({lot_id:row.current_inventory_lot_id || row.inventory_lot_id,location_id:row.current_warehouse_location_id}); },pageAllowed: () => true, showToast: () => { throw Error('Unexpected denial'); }};
context.productionCurrentLocationClickable = method('productionCurrentLocationClickable');
const open = method('openProductionInventory');
const row = {status:'posted', is_fully_delivered:true, current_inventory_status:'located',
  current_warehouse_location_id:42, inventory_lot_id:1, current_inventory_lot_id:2};
assert(context.productionCurrentLocationClickable(row), 'Delivered order still has physical stock');
open.call(context, row);
assert.equal(new URL(opened, 'http://localhost').searchParams.get('lot_id'), '2');
assert.equal(new URL(opened, 'http://localhost').searchParams.get('location_id'), '42');
assert(!context.productionCurrentLocationClickable({...row, current_warehouse_location_map_issue:'位置需核对'}));
assert(!context.productionCurrentLocationClickable({...row, status:'reversed'}));
context.pageAllowed = () => false;
assert(!context.productionCurrentLocationClickable(row));
console.log('PASS current inventory navigation follows the visible lot, including delivered orders; permissions and location guards retained');

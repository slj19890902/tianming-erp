const test=require('node:test'),assert=require('node:assert/strict');
require('../../../static/ui/warehouse-movement-view.js');
test('all user-controlled fields are escaped, quantities and movement locations remain readable',()=>{
 const row=WarehouseMovementView.row({customer_name:'<img src=x onerror=alert(1)>',inventory_code:'A&B',reason_display:'<script>x</script>',product_name:'纸盒',before_physical:100,after_physical:120,physical_delta:20,from_location:'A1-1-1',to_location:'B2-2-1',display_unit:'boxes',quantity_scope:'本次涉及库存，非全仓合计'},()=>'<time>');
 assert.ok(!row.includes('<img')&&!row.includes('<script>')&&!row.includes('<time>'));
 assert.ok(row.includes('A&amp;B')&&row.includes('100 → 120 只')&&row.includes('→ B2-2-1'));
});
test('reservations show unchanged physical balance and historical ids stay inside closed details',()=>{
 const row=WarehouseMovementView.row({before_physical:100,after_physical:100,physical_delta:0,lot_number:'BATCH-ID',movement_number:'RECORD-ID',before_available:100,after_available:70,before_reserved:0,after_reserved:30},()=>'-');
 assert.ok(row.includes('实存不变')&&row.includes('可用 100 → 70')&&row.includes('订单占用 0 → 30'));
 const details=row.match(/<details[\s\S]*?<\/details>/)[0];assert.ok(details.includes('BATCH-ID')&&details.includes('RECORD-ID'));assert.ok(!details.includes(' open'));
});

const test=require('node:test'),assert=require('node:assert/strict');
const {rows,columns}=require('../../static/ui/material-strip-print.js');
test('12 columns and same-size components remain separate; parent quantities not summed',()=>{
  const c={product_code:'80012753',strip_finished_quantity:1600,order_set_quantity:1600,finished_unit:'套',requisition_quantity:267,requisition_unit:'张',report_width_mm:236,report_length_mm:750,component_label:'A片',specification:'210×354=3',customer_category:'A'};
  const result=rows([{customer_name:'研光',components:[c,{...c,component_label:'2B+2C',requisition_quantity:800}]}]);
  assert.equal(columns.length,12);assert.equal(result.length,2);assert.equal(result[0].length,12);
  assert.equal(result[0][3],'1600套');assert.equal(result[1][3],'1600套');
  assert.equal(result[0][9],'267 张');assert.equal(result[1][9],'800 张');
});
test('inventory source is independent and uses actual lot dimensions/location',()=>{
  const result=rows([{customer_name:'研光',components:[{requisition_quantity:10,inventory_pick_lines:[{reservation_id:3,kind:'semi_finished',quantity:5,unit:'张',report_width_mm:600,report_length_mm:800,location_name:'二楼B区B3架第2层第1格',lot_number:'L1'}]}]}]);
  assert.equal(result.length,2);assert.equal(result[1][7],600);assert.match(result[1][11],/B3架第2层第1格/);
});
test('no BOM or mold inference and customer-safe strips hide location',()=>{
  const result=rows([{components:[{requisition_quantity:1,needs_die_cut:true,mold_location:'内部位置'}]}],true);
  assert.equal(result[0][3],'成品数量待核对');assert.equal(result[0][4],'内部资料');assert.match(result[0][6],/待核对/);
});
test('actual receipt variances never masquerade as a plan strip',()=>{
  assert.throws(()=>rows([{paper_phase:'actual_receipt',components:[]}]),/实收差异/);
});

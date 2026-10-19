const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../static/ui/manual-sheet-deduction.js');
const stock=(id=1)=>({lot_id:id,version:1,selectable:true,max_stock_quantity:25,available_stock_quantity:80,
  stock_yield_per_sheet:4,review_hash:'a'.repeat(64),warning_codes:[]});
const preview=(items=[stock()])=>({expected_order_item_revision:'b'.repeat(64),remaining_required_piece_qty:100,
  pieces_per_box:1,match_mode:'exact',can_further_match:false,items});
function state() {
  ui.pending.clear();
  const value={actorId:1,orderItemId:1,snapshotId:0,componentType:'whole',...ui.component.data(),...ui.component.methods,
    emitted:[],$emit(name,data){this.emitted.push({name,data})}};
  Object.defineProperty(value,'identity',{get:()=>ui.component.computed.identity.call(value)});
  return value;
}
test('opening requests original axes; swaps require explicit empty-result action',async()=>{
  const reads=[];global.axios={get:async(url,options)=>{reads.push(options.params);return {data:preview()}}};
  const s=state();await s.open();await s.load('swapped');
  assert.deepEqual(reads,[{component_type:'whole',match_mode:'exact'}]);
  s.rows=[];s.preview.can_further_match=true;
  await s.load('swapped');assert.equal(reads.at(-1).match_mode,'swapped');
});
test('selection suggests only uncovered sheets and keeps per-box pieces separate',async()=>{
  global.axios={get:async()=>({data:preview([stock(1),stock(2)])})};
  const s=state();await s.open();s.rows[0].selected=true;s.choose(s.rows[0]);
  assert.equal(s.rows[0].inputQuantity,25);assert.equal(s.totals().remaining,0);
  s.rows[0].inputQuantity=10;s.rows[1].selected=true;s.choose(s.rows[1]);
  assert.equal(s.rows[1].inputQuantity,15);assert.equal(s.totals().stock,25);assert.equal(s.totals().valid,true);
  s.preview.pieces_per_box=2;assert.equal(s.totals().boxes,50);
  assert.match(s.yieldText(s.rows[0]),/每2片箱片做1只/);
  s.rows[1].inputQuantity=16;assert.equal(s.totals().valid,false);
});
test('multi-lot confirmation is one immutable request and uncertain replay keeps it intact',async()=>{
  let writes=[];global.axios={get:async()=>({data:preview([stock(1),stock(2)])}),post:async(url,payload)=>{
    writes.push({url,payload:structuredClone(payload)});if(writes.length===1)throw Error('lost');
    return {data:{message:'成功',remaining_required_piece_qty:0,stock_quantity:25}};
  }};
  const s=state();await s.open();for(const row of s.rows){row.selected=true;row.inputQuantity=row.lot_id===1?10:15;}
  await s.confirmSelection();assert.equal(writes.length,1);assert.match(writes[0].url,/manual-reserve-batch$/);
  assert.equal(writes[0].payload.items.length,2);assert.equal(s.uncertain,true);
  s.close();await s.open();s.rows[0].inputQuantity=1;await s.submitHeld();
  assert.deepEqual(writes[0],writes[1]);assert.equal(s.opened,false);assert.equal(ui.pending.size,0);
});
test('cancelled read cannot populate a closed or reopened dialog and never reserves',async()=>{
  let finish,writes=0;global.axios={get:()=>new Promise(resolve=>finish=resolve),post:async()=>writes++};
  const s=state(),opened=s.open();s.close();finish({data:preview()});await opened;
  assert.equal(s.opened,false);assert.equal(s.preview,null);assert.equal(writes,0);
});
test('only selected valid positive integer quantities are submitted',async()=>{
  let writes=0;global.axios={get:async()=>({data:preview()}),post:async()=>writes++};
  const s=state();await s.open();for(const value of ['',0,-1,1.5,26]) {
    s.rows[0].selected=true;s.rows[0].inputQuantity=value;await s.confirmSelection();
  }
  assert.equal(writes,0);
});

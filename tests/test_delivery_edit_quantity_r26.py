from pathlib import Path
import json
from tests.test_p1_15_delivery_draft_frontend import _run_node, _vue_harness

def _harness(assertions):
    source=(Path(__file__).parents[1]/"static/ui/order-reference.js").read_text("utf-8")
    return _vue_harness(assertions).replace("vm.createContext(sandbox);", "vm.createContext(sandbox);\nvm.runInContext("+json.dumps(source)+", sandbox);")


def test_pending_and_dispatched_edit_use_original_current_quantity_basis_once():
    result = _run_node(_harness('''
const cases = [
  {status:'pending', saved:10, remaining:10, orderRemaining:10, available:10, expected:[10,10,10]},
  {status:'pending', saved:4, remaining:10, orderRemaining:10, available:10, expected:[10,10,10]},
  {status:'pending', saved:4, remaining:7, orderRemaining:10, available:7, expected:[7,10,7]},
  {status:'pending', saved:4, remaining:0, orderRemaining:10, available:0, expected:[0,10,0]},
  {status:'dispatched', saved:4, remaining:6, orderRemaining:6, available:6, expected:[10,10,10]},
  {status:'dispatched', saved:4, remaining:2, orderRemaining:6, available:2, expected:[6,10,6]},
];
(async () => {
  for (const sample of cases) {
    const item = {id:26,order_item_id:260,delivered_quantity:sample.saved,product_code:'R26-FAKE',customer_po:'R26-PO'};
    const pending = {item_id:260,remaining_quantity:sample.remaining,order_remaining_quantity:sample.orderRemaining,deliverable_quantity:sample.available};
    const context = {
      deliveryForm:{}, pendingDeliveryItems:[pending],
      loadDeliveryPendingItems:async()=>true,
      deliverySourceModeFromLines:()=> 'order',
      createDeliveryLine:value=>value,
      deliveryFormSignature:()=> 'unchanged-source',
      resetDeliveryReminderState(){},
      loadDeliveryRemindersForCustomer:async()=>{},
    };
    await methods.editDelivery.call(context,{id:2600,status:sample.status,customer_id:26,detail_loaded:true,items:[item],version:1});
    const line=context.deliveryForm.lines[0];
    const actual=[line.remaining_quantity,line.order_remaining_quantity,line.deliverable_quantity];
    if(JSON.stringify(actual)!==JSON.stringify(sample.expected)) throw new Error(sample.status+' quantity duplicated: '+JSON.stringify(actual)+' expected '+JSON.stringify(sample.expected));
    if(line.delivered_quantity!==sample.saved || line.order_item_id!==260 || line.customer_po!=='R26-PO') throw new Error('saved quantity or source binding changed');
  }
  if(writes.length) throw new Error('opening edit must never write');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''))
    assert result.returncode == 0, result.stderr


def test_unordered_finished_edit_keeps_saved_allocation_and_quantity_contract():
    result = _run_node(_harness('''
(async () => {
 const item={id:26,source_type:'unordered_finished',product_id:260,customer_po:'R26-STOCK',delivered_quantity:2,quantity_contract:{customer_unit:'套',physical_unit:'PCS',physical_quantity:10},allocations:[{inventory_lot_id:26,quantity:10}]};
 const context={deliveryForm:{},deliverySourceModeFromLines:()=> 'unordered_finished',createDeliveryLine:v=>v,deliveryFormSignature:()=> 'stock-source',resetDeliveryReminderState(){},loadDeliveryRemindersForCustomer:async()=>{},loadDeliveryPendingItems:async()=>{throw new Error('unordered stock must not inherit order pending quantities');}};
 await methods.editDelivery.call(context,{id:2600,status:'pending',customer_id:26,detail_loaded:true,items:[item],version:1});
 const line=context.deliveryForm.lines[0];
 if(line.delivered_quantity!==2 || line.quantity_contract.physical_quantity!==10 || line.allocations[0].quantity!==10 || line.allocations[0].inventory_lot_id!==26) throw new Error('stock quantity conversion or allocation changed');
 if(writes.length) throw new Error('opening edit must not write');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''))
    assert result.returncode == 0, result.stderr

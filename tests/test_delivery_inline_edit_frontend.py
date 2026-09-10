from tests.test_p1_15_delivery_draft_frontend import _run_node, _vue_harness


def test_inline_edit_routes_po_independently_and_preserves_batch_stock_and_retry():
    result = _run_node(_vue_harness(r'''
const assert=require("assert");
const c={...methods,canDelivery:true,deliveryInlineEdit:null,deliveryOperationState:{},receiptOperationState:{},expandedDeliveryRows:{},showToast(){},hasPermission(){return true;},errorMessage(e){return e.response?.data?.detail || e.message;}};
const item={id:31,source_type:"unordered_finished",product_id:6,delivered_quantity:600,unit_price:1.5,customer_po:"OLD",inventory_sources:[{inventory_lot_id:1,quantity:300},{inventory_lot_id:2,quantity:300}]};
const other={id:32,source_type:"order",order_item_id:40,delivered_quantity:30};
const row={id:9,version:2,status:"dispatched",source_mode:"mixed",items:[item,other]};
let calls=[];
sandbox.axios.put=async(url,payload)=>{calls.push([url,payload]);return {data:{version:3,items:row.items}};};
(async()=>{
 c.startInlineDeliveryLine(row,item); c.deliveryInlineEdit.customer_po="NEW";
 assert(await c.saveInlineDeliveryLine(row,item));
 assert.equal(calls[0][0],"/api/deliveries/9/customer-po");
 assert.equal(calls[0][1].items.length,1);
 assert.equal(calls[0][1].items[0].customer_po,"NEW");
 c.startInlineDeliveryLine(row,item); c.deliveryInlineEdit.delivered_quantity=450;
 c.rebalanceInlineDeliveryAllocations();
 assert.deepEqual(c.deliveryInlineEdit.allocations.map(a=>a.quantity),[300,150]);
 assert(await c.saveInlineDeliveryLine(row,item));
 assert.equal(calls[1][0],"/api/deliveries/9/revision");
 assert.deepEqual(calls[1][1].items[0].allocations.map(a=>a.quantity),[300,150]);
 assert.equal(calls[1][1].items[1].delivered_quantity,30);
 assert.equal("customer_po" in calls[1][1].items[0],false);
 c.startInlineDeliveryLine(row,item);
 c.deliveryInlineEdit.allocations[0].quantity=250;c.deliveryInlineEdit.allocations[1].quantity=350;
 c.sumInlineDeliveryAllocations();assert.equal(c.deliveryInlineEdit.delivered_quantity,600);
 assert(await c.saveInlineDeliveryLine(row,item));assert.equal(calls[2][0],"/api/deliveries/9/revision");
 assert.deepEqual(calls[2][1].items[0].allocations.map(a=>a.quantity),[250,350]);
 row.return_receipt_status="confirmed";
 c.startInlineDeliveryLine(row,item); assert.equal(c.deliveryInlineEdit.quantityEditable,false);
 c.deliveryInlineEdit.delivered_quantity=400;
 assert.equal(await c.saveInlineDeliveryLine(row,item),false);
 assert.equal(calls.length,3); c.cancelInlineDeliveryLine();
 row.return_receipt_status=null;
 c.startInlineDeliveryLine(row,item); c.deliveryInlineEdit.delivered_quantity=0;
 assert.equal(await c.saveInlineDeliveryLine(row,item),false);assert.equal(calls.length,3);
 c.deliveryInlineEdit.delivered_quantity=500;c.rebalanceInlineDeliveryAllocations();
 sandbox.axios.put=async(url,payload)=>{calls.push([url,payload]);throw new Error("offline");};
 assert.equal(await c.saveInlineDeliveryLine(row,item),false);
 assert.equal(c.deliveryInlineEdit.uncertain,true);
 const attempt=c.deliveryInlineEdit.attempt;
 assert.equal(await c.saveInlineDeliveryLine(row,item),false);
 assert.equal(c.deliveryInlineEdit.attempt,attempt);
 assert.equal(calls[3][1].idempotency_key,calls[4][1].idempotency_key);
 let printCalled=false;c.printDelivery=()=>{printCalled=true;};
 assert.equal(c.runDeliveryPrimaryRowAction(row),false);assert.equal(printCalled,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''))
    assert result.returncode == 0, result.stderr

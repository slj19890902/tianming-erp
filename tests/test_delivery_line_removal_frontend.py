from pathlib import Path
import shutil
import subprocess


def test_quantity_selects_common_box_and_delivery_removal_preserves_remaining_line(tmp_path):
    source = (Path(__file__).parents[1] / "static/index.html").read_text(encoding="utf-8")
    def method(start, end):
        return source[source.index(start):source.index(end, source.index(start))].strip().rstrip(",")
    methods = ",\n".join([
        method("toggleOrderCommonBoxSelection(product, checked) {", "setOrderCommonBoxQuantity(product, value) {"),
        method("setOrderCommonBoxQuantity(product, value) {", "orderCommonBoxMaterialText(product) {"),
        method("async deleteDeliveryDetailLine(row, item) {", "async deleteDelivery(row) {"),
    ])
    script = "const methods={" + methods + "};\n" + r'''
const assert = require("node:assert/strict");
const confirm = () => true;
const createIdempotencyKey = () => "test-remove-line-idempotency";
let request;
const axios = {put:async (url,payload) => {
  request={url,payload};
  return {data:{id:7,version:4,items:[{id:9,product_id:3,delivered_quantity:20}]}};
}};
(async () => {
 const ctx={...methods,orderCommonBoxPicker:{selected:{},selection_sequence:0}};
 ctx.setOrderCommonBoxQuantity({id:3},"20");
 assert.equal(ctx.orderCommonBoxPicker.selected["3"].quantity,"20");
 assert.equal(ctx.orderCommonBoxPicker.selection_sequence,1);
 ctx.setOrderCommonBoxQuantity({id:3},"21");
 assert.equal(ctx.orderCommonBoxPicker.selection_sequence,1);
 const kept={id:9,source_type:"unordered_finished",product_id:3,delivered_quantity:20,unit_price:3.6,customer_po:"PO-SNAPSHOT",allocations:[{inventory_lot_id:8,planned_quantity:20}]};
 const removed={id:8,product_id:2,delivered_quantity:5};
 const row={id:7,version:3,status:"dispatched",delivery_number:"UAT-7",items:[removed,kept]};
 Object.assign(ctx,{deliveryOperationState:{},receiptOperationState:{},deliveries:[row],expandedDeliveryRows:{},deliverySourceModeFromLines:()=>"unordered_finished",showToast:()=>{},loadDeliveries:async()=>true,errorMessage:e=>String(e)});
 assert.equal(await ctx.deleteDeliveryDetailLine(row,removed),true);
 assert.equal(request.url,"/api/deliveries/7/revision");
 assert.equal(request.payload.expected_version,3);
 assert.equal(request.payload.items.length,1);
 assert.equal(request.payload.items[0].product_id,3);
 assert.equal(request.payload.items[0].unit_price,3.6);
 assert.deepEqual(request.payload.items[0].allocations,[{inventory_lot_id:8,quantity:20}]);
 assert.equal("customer_po" in request.payload.items[0],false);
 assert.equal(row.items.length,1);
 assert.equal(ctx.expandedDeliveryRows[7],true);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    path = tmp_path / "delivery-line-removal.cjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([shutil.which("node") or "node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    picker = source[source.index('<div v-if="orderCommonBoxPicker.visible"'):source.index('<div v-if="masterChangeConfirm.visible"')]
    assert '@click.self="closeOrderCommonBoxPicker"' not in picker
    assert ':disabled="!isOrderCommonBoxSelected(row.id)"' not in picker

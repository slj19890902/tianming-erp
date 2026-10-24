const test = require('node:test');
const assert = require('node:assert/strict');
const structure = require('../../static/js/product-structure-edit.js');

test('shared BOM to ordinary clears the current recipe and preserves both price fields', () => {
  const form = {id:4,customer_id:2,product_code:'80011641',box_style:'A1',unit:'套',
    shared_catalog_policy_enabled:true,sale_unit_price:8,sale_unit_price_no_tax:7,
    is_composite:true,is_virtual_composite_parent:false};
  const editor = {enabled:true,inventory_mode:'assembled',expected_version:12,
    components:[{component_product_id:9,quantity_per_set:2,inventory_relation:'assembly'}]};
  structure.sharedBomTransition(form,editor,'BOM组合');
  assert.deepEqual(editor.components,[]);
  assert.equal(editor.enabled,false);
  assert.equal(editor.inventory_mode,'legacy');
  assert.equal(editor.expected_version,12);
  assert.equal(form.unit,'只');
  assert.equal(form.is_composite,false);
  assert.deepEqual([form.customer_id,form.product_code,form.sale_unit_price,form.sale_unit_price_no_tax],[2,'80011641',8,7]);
  form.box_style='BOM组合';
  structure.sharedBomTransition(form,editor,'A1');
  assert.deepEqual(editor.components,[{component_product_id:9,quantity_per_set:2,inventory_relation:'assembly'}]);
  editor.components[0].quantity_per_set=5;
  assert.equal(editor._shared_assembled_draft[0].quantity_per_set,2);
});

test('unshared and limited-stock relationships retain their existing editor contract', () => {
  for(const flag of [false,undefined]) {
    const form={box_style:'A1',shared_catalog_policy_enabled:flag,unit:'套'};
    const editor={enabled:true,inventory_mode:'assembled',components:[{component_product_id:9}]};
    structure.sharedBomTransition(form,editor,'BOM组合');
    assert.equal(editor.enabled,true);assert.equal(form.unit,'套');assert.equal(editor.components.length,1);
  }
});

test('older confirmed manufacturing groups use the real legacy payload on return to ordinary',()=>{
  const form={id:1,box_style:'A1',shared_manufacturing_enabled:true,shared_catalog_policy_enabled:false,unit:'套'};
  const editor={enabled:true,inventory_mode:'assembled',components:[{component_product_id:2}]};
  structure.sharedBomTransition(form,editor,'BOM组合');
  assert.equal(editor.enabled,false);
  assert.equal(editor.inventory_mode,'legacy');
  assert.deepEqual(editor.components,[]);
});

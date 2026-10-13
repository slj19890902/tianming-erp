const {test}=require('node:test');
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const html=fs.readFileSync(path.resolve(__dirname,'../../static/index.html'),'utf8');
function context(plans) {
  const c={stockReplenishmentForm:{source_type:'stock_warning', stock_now:false, replenishment_plans:plans,
    items:[1,2].map((id)=>({stock_policy_id:31,reference_product_id:id,customer_id:1,
      material_supplier_name:id===1?'森林阳光':'欢达',target_inventory_type:'semi_finished',quantity:20,
      material_code:'M'+id,layer_count:5,flute_type:'AB',report_length_mm:500,report_width_mm:300,crease_type:'净料'}))}};
  for(const name of ['stockReplenishmentMaterialSuppliers','stockReplenishmentHasSplitBomSuppliers','validateStockReplenishmentForm']) {
    const m=html.match(new RegExp('^\\s*'+name+'\\([^\\n]*\\) \\{','m'));
    assert.ok(m,name); const end=html.indexOf('\n          },',m.index);
    c[name]=vm.runInNewContext('({'+html.slice(m.index,end+13).trim().replace(/,$/,'')+'})')[name];
  }
  return c;
}
test('完整BOM保留两家供应商，解除保存拦截并提示按组件报料',()=>{
  const c=context([{policy_id:31,components:[{product_id:1},{product_id:2}]}]);
  assert.equal(c.stockReplenishmentHasSplitBomSuppliers(),true);
  assert.equal(c.validateStockReplenishmentForm(),'');
  assert.match(html,/按各组件供应商分别报料，补库套数保持一致/);
});
test('缺少BOM组件合同或普通混合供应商仍不放行',()=>{
  for(const plans of [[],[{policy_id:31,components:[{product_id:1}]}]]) {
    const c=context(plans);
    assert.equal(c.stockReplenishmentHasSplitBomSuppliers(),false);
    assert.match(c.validateStockReplenishmentForm(),/不同供应商的普通补库请分开保存/);
  }
});
test('多供应商BOM仍保留数量校验',()=>{
  const c=context([{policy_id:31,components:[{product_id:1},{product_id:2}]}]);
  c.stockReplenishmentForm.items[1].quantity=0;
  assert.match(c.validateStockReplenishmentForm(),/第 2 行数量必须是大于0的整数/);
});

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const html = fs.readFileSync('static/index.html', 'utf8');
function method(name) {
  const match = new RegExp('^          (?:async )?' + name + '\\(', 'm').exec(html);
  assert.ok(match, name);
  const next = /^          (?:async )?[A-Za-z_$][\w$]*\(/m.exec(html.slice(match.index + 12));
  return html.slice(match.index, match.index + 12 + next.index);
}
const constants = html.slice(html.indexOf('      let bomComponentKeyCounter'), html.indexOf('      const blankMaterial ='));
const names = ['validateProductBom','onProductBoxStyleChange','onBomInventoryModeChange',
  'applyBomResponse','normalizeBomComponent','_productBomSaveFields','_productBomDirty','bomComponentUnit'];
// Optional while running the pre-fix regression, mandatory after implementation.
for (const name of ['productBomSetIssues','repairProductBomSet','focusProductBomEditor']) {
  if (html.includes('          '+name+'(')) names.push(name);
}
const methods = Function(constants+'return ({'+names.map(method).join('\n')+'});')();
function context() {
  return {...methods,canEditProducts:true,productForm:{id:3586,customer_id:137,box_style:'BOM组合',unit:'套',
    combination_mode:'parent_priced_set',is_virtual_composite_parent:false,composite_fulfillment_mode:'parent_delivery'},
    bomEditor:{enabled:true,loading:false,inventory_mode:'assembled',persisted_inventory_mode:'legacy',components:[
      {component_product_id:3587,quantity_per_set:1,inventory_relation:'assembly',spare_sheet_quantity:0,mold_max_yield_per_sheet:null},
      {component_product_id:3588,quantity_per_set:2,inventory_relation:'assembly',spare_sheet_quantity:0,mold_max_yield_per_sheet:null}]},
    normalizeBoxTypeDisplay:x=>x,applyPartnerProductDefaults:()=>{},mergeBomComponentOptions:()=>{},
    $nextTick:fn=>fn(),$refs:{},showToast:()=>{}};
}
test('valid 12500 draft preserves quantities and rejects invalid child quantities',()=>{
  const c=context(); assert.equal(c.validateProductBom(),'');
  c.bomEditor.components[1].quantity_per_set=0;
  assert.match(c.validateProductBom(),/数量|用量/);
});
test('hidden conflicts name the field and an actionable repair entry',()=>{
  for(const [group,key,value,label] of [
    ['bomEditor','inventory_mode','legacy','产品形成方式'],['productForm','unit','片','父件单位'],
    ['productForm','combination_mode','component_priced','计价方式'],
    ['productForm','is_virtual_composite_parent',true,'虚拟父件'],
    ['productForm','composite_fulfillment_mode','component_delivery','交付方式']]) {
    const c=context();c[group][key]=value;const error=c.validateProductBom();
    assert.ok(error.includes(label),error);assert.match(error,/应用组套设置/);
  }
});
test('repair changes draft only and keeps child ids and ratios',()=>{
  const c=context();c.productForm.unit='片';c.bomEditor.inventory_mode='legacy';
  c.repairProductBomSet();assert.equal(c.validateProductBom(),'');
  assert.deepEqual(c.bomEditor.components.map(x=>[x.component_product_id,x.quantity_per_set]),[[3587,1],[3588,2]]);
  assert.equal(c.productForm.unit,'套');
});
test('loading and failed loads block save and repair without changing draft',()=>{
  for(const state of [{loading:true},{error:'读取失败'}]) {
    const c=context();Object.assign(c.bomEditor,state);
    assert.match(c.validateProductBom(),/加载|读取/);
    const before=JSON.stringify(c.productForm);c.repairProductBomSet();assert.equal(JSON.stringify(c.productForm),before);
  }
});
test('legacy subkit and read-only permissions cannot be bypassed by repair',()=>{
  for(const state of ['subkit','readonly']) {
    const c=context();c.productForm.unit='片';
    if(state==='subkit') c.bomEditor.subkit={enabled:true};else c.canEditProducts=false;
    const before=JSON.stringify(c);c.repairProductBomSet();assert.equal(JSON.stringify(c),before);
  }
});
test('DOM exposes repair and retry and disables editing before BOM load',()=>{
  for(const pattern of [/ref="productBomPanel"/,/@click="repairProductBomSet"/,
    /@click="loadProductBom\(productForm.id\)"/,/:disabled="bomEditor.loading[^"\n]*"[^>]*v-model="productForm.box_style"/,
    /this.resetBomEditor\(\);\s*this.bomEditor.loading = !!openedId;/]) assert.ok(pattern.test(html),String(pattern));
});
test('save locates BOM before any product preview/write',()=>{
  const start=html.indexOf('            if (masterSaveEntity === "product" && (this.bomEditor.loading');
  assert.ok(start>=0);
  const end=html.indexOf('            if (masterSaveEntity && this.masterSavePending)',start);
  const guard=Function('masterSaveEntity',html.slice(start,end));
  const c=context();c.productForm.unit='片';let focused=0,toast='';
  c.focusProductBomEditor=()=>focused++;c.showToast=x=>toast=x;
  assert.equal(guard.call(c,'product'),false);assert.equal(focused,1);assert.match(toast,/父件单位/);
});
test('new BOM parent uses combined atomic save; ordinary new product remains ordinary',()=>{
  const c=context();c.productForm.id=null;c.modal={type:'product'};
  assert.equal(c._productBomDirty(),true);
  c.bomEditor.enabled=false;assert.equal(c._productBomDirty(),false);
});
test('async BOM response then explicit set selection preserves loaded recipe',async()=>{
  const waiting=[];const controllers=new Map();
  const load=Function('axios','latestRequestControllers','return ({'+method('loadProductBom')+'});')(
    {get:()=>new Promise(resolve=>waiting.push(resolve))},controllers).loadProductBom;
  const c=context();c.productForm.box_style='模切内盒';
  c.beginLatestRequest=key=>{const token={signal:{}};controllers.set(key,token);return token;};
  c.finishLatestRequest=(key,token)=>{if(controllers.get(key)===token)controllers.delete(key);};
  const pending=load.call(c,3586);assert.equal(c.bomEditor.loading,true);
  assert.match(c.validateProductBom(),/加载/);
  waiting[0]({data:{version:4,inventory_mode:null,is_composite:true,components:c.bomEditor.components}});
  assert.equal(await pending,true);assert.equal(c.bomEditor.loading,false);
  c.productForm.box_style='BOM组合';c.onProductBoxStyleChange();
  assert.equal(c.validateProductBom(),'');assert.equal(c.bomEditor.expected_version,4);
  assert.deepEqual(c.bomEditor.components.map(x=>x.quantity_per_set),[1,2]);
});

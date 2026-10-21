// Run the actual common-box computed editor and save payload without a browser.
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const root = process.argv[2], html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(x => x[1]).filter(x => x.trim());
const box = {
  axios: {defaults: {}, interceptors: {request: {use() {}}, response: {use() {}}}},
  Vue: {createApp(d) {box.d = d; return {component() {return this;}, mount() {return this;}};}},
  window: {}, document: {}, localStorage: {getItem() {return '';}, setItem() {}, removeItem() {}},
  TMOrderReference: {component: {}}, URLSearchParams, setTimeout, clearTimeout, console,
};
vm.createContext(box);
vm.runInContext(fs.readFileSync(path.join(root, 'static/ui/incoming-actual.js'), 'utf8'), box);
vm.runInContext(scripts[0].slice(0, scripts[0].indexOf('window.TmDeliveryEditorLayout.install(app);')), box);
const editor = box.d.computed.productExternalDeliverySpecification;
assert.ok(editor && editor.get && editor.set, 'External customer specification needs an editable, saveable control');
assert.match(html, /v-model="productExternalDeliverySpecification"/);
assert.match(html, /v-if="productForm.supply_mode!=='external_purchase'"[^>]*>\s*<label>客户规格原文/);
assert.doesNotMatch(html, /<label>客户规格<\/label><input[^>]*readonly/);
const ctx = {
  ...box.d.methods,
  productForm: {
    supply_mode: 'external_purchase', external_packaging_category_code: 'other_packaging',
    external_packaging_specification_summary: '客户包材 800×180×60mm', customer_specification: null,
    external_packaging_default_order_quantity_basis: 1, external_packaging_default_purchase_quantity_basis: 2,
    _external_specification: {summary: '客户包材 800×180×60mm'},
    _external_selected_ids: [7], _external_default_product_id: 7,
    product_code: 'EXT-1', customer_material_code: 'EXT-1', _production_processes: [],
  },
  productPrintingWriteFields: () => ({printing_plate_mode: 'no_plate'}),
  serializeProductionProcesses: rows => (rows || []).join(','), normalizeMmInteger: Number,
  attachMasterUpdateMetadata: (_entity, payload) => payload,
};
const before = ctx.buildProductWritePayload();
assert.equal(editor.get.call(ctx), '客户包材 800×180×60mm');
assert.equal(ctx.productForm.customer_specification, null, 'Opening the editor must not freeze its automatic default');
ctx.productForm.external_packaging_specification_summary = '客户包材 900×180×60mm';
assert.equal(editor.get.call(ctx), '客户包材 900×180×60mm');
editor.set.call(ctx, '90×18×6cm，客户专用包装');
const custom = ctx.buildProductWritePayload();
assert.equal(custom.customer_specification, '90×18×6cm，客户专用包装');
assert.equal(JSON.stringify(custom.external_supply), JSON.stringify(before.external_supply));
assert.equal(custom.external_packaging_default_purchase_quantity_basis, 2);
ctx.productForm.external_packaging_specification_summary = '修改后的采购规格';
assert.equal(editor.get.call(ctx), custom.customer_specification, 'A physical-spec edit must not overwrite custom delivery wording');
editor.set.call(ctx, '');
assert.equal(ctx.buildProductWritePayload().customer_specification, '');
assert.equal(editor.get.call(ctx), '');
ctx.productForm.customer_specification = null;
assert.equal(editor.get.call(ctx), '修改后的采购规格');
assert.equal(ctx.buildProductWritePayload().customer_specification, null);
console.log('External customer specification: edit/save payload, automatic default, explicit blank, and purchasing isolation passed');

const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('          hydrateProductForm(detail) {');
const end = html.indexOf('          drawingDisplayName(drawing) {', start);
const methods = vm.runInNewContext('({' + html.slice(start, end) + '})', {blankProduct: () => ({})});
const context = {
  mergePrintingPlateOptions() {}, hydrateProductPrintingPlateRows() {},
  parseProductPrintingColors: () => [], normalizeBoxTypeDisplay: v => v,
  productBoxTypeRule: () => null, normalizeCuttingMode: v => v || '一开一',
  usesProductDefaultCuttingMode: () => false, parseProductionProcesses: () => [],
  unmanagedProductionProcesses: () => [], normalizeProductDimensions: v => v,
};
const detail = {id: 10, customer_id: 1, box_style: 'ZHJ 纸护角', supply_mode: 'corrugated_production',
  external_supply: {specification: {}, candidates: []},
  classification: {kind: 'external', needs_supply_completion: true, category_code: 'paper_corner_guard'}};
const original = JSON.stringify(detail);
const form = methods.hydrateProductForm.call(context, detail);
assert.equal(form.box_style, '其他');
assert.equal(form.supply_mode, 'external_purchase');
assert.equal(form.external_packaging_category_code, 'paper_corner_guard');
assert.equal(form._external_selected_ids.length, 0);
assert.equal(form._external_default_product_id, null);
assert.equal(Object.keys(form._external_specification).length, 0);
assert.equal(form._external_ratio_mode, '');
assert.equal(JSON.stringify(detail), original);
const paper = methods.hydrateProductForm.call(context, {box_style:'NH 天华内盒1',report_length_mm:900,report_width_mm:500,
  classification:{kind:'box',box_style:'异形箱'}});
assert.equal(paper.box_style, '异形箱');
assert.equal(paper.report_length_mm, 900);
assert.equal(paper.report_width_mm, 500);
for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  if (!m[1].includes('src=')) new vm.Script(m[2]);
}
console.log('Classification form hydration, missing supply values, preserved dimensions and inline JS passed.');

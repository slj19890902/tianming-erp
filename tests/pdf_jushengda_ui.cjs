const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
function method(start, next) {
  const body = source.slice(source.indexOf(`          ${start}`), source.indexOf(`          ${next}`, source.indexOf(`          ${start}`)));
  return Object.values(Function(`return ({${body}})`)())[0];
}
const context = {
  isImportDraftLocked: draft => ['saving', 'success', 'unknown'].includes(draft?._save_status),
  preparePdfImportItem: item => ({...item, prepared:true}),
  manualPdfDraftItem: line_no => ({line_no, quantity:null}),
  manualPdfReviewFacts: method('manualPdfReviewFacts(draft)', 'manualPdfDraftItem(lineNo)'),
};
const begin = method('startManualPdfDraft(draft)', 'addManualPdfDraftItem(draft)');
const invalidate = method('invalidateImportDraftConfirmation(draft)', 'importDraftSaveStatusClass(draft)');
const item = {line_no:1, quantity:90, unit_price:'20.5', amount:'1845', production_notes:'送到指定地址', raw_product_code:'', matched_product_id:null};
const draft = {template:'jushengda_purchase_v1', customer_po:'JSD20261022001', order_date:'2026-10-22', delivery_date:'2026-10-27', items:[item], confirmed:true, integrity_check:{integrity_status:'unknown'}};
begin.call(context, draft);
assert.equal(draft.items.length, 1);
assert.equal(draft.items[0].quantity, 90);
assert.equal(draft.items[0].production_notes, item.production_notes);
assert.equal(draft.items[0].amount, '1845');
assert.notEqual(draft.items[0], item);
assert.equal(draft.confirmed, false);
assert.equal(draft._show_advanced_details, true);
const empty = {items:[]};
begin.call(context, empty);
assert.equal(empty.items[0].quantity, null);
draft.integrity_check.integrity_status = 'manual_confirmed';
draft._manual_review_facts = context.manualPdfReviewFacts(draft);
draft.items[0]._inventory = {quantity:999};
invalidate.call(context, draft);
assert.equal(draft.integrity_check.integrity_status, 'manual_confirmed');
draft.items[0].quantity = 91;
invalidate.call(context, draft);
assert.equal(draft.integrity_check.integrity_status, 'unknown');
const locked = {...draft, _save_status:'saving', confirmed:true};
invalidate.call(context, locked);
assert.equal(locked.confirmed, true);
const candidateTag = source.split('\n').find(line => line.includes('class="order-item-sub-row import-product-candidate-row"'));
const condition = candidateTag.match(/v-if="([^"]+)"/)[1];
const visible = Function('draft', 'item', `return (${condition})`);
assert.equal(visible(draft, {...item, is_new_product:false, product_candidates:[{id:1}]}), true);
assert.equal(visible(draft, {...item, product_candidates:[]}), false);
assert.equal(visible({...draft, template:'legacy'}, {...item, product_candidates:[{id:1}]}), false);
const manualTag = source.split('\n').find(line => line.includes('v-if="!draft.manual_entry &&'));
const manualVisible = Function('draft', `return (${manualTag.match(/v-if="([^"]+)"/)[1]})`);
assert.equal(manualVisible({...draft, manual_entry:false, recognition_status:'needs_confirmation'}), true);
assert.match(source, /v-model="item.amount"[^>]+@input="invalidateImportDraftConfirmation\(draft\)"/);
console.log('PDF manual review UI: preserve facts, explicit one-candidate choice, editable source amount, recheck edits, and lock protection passed');

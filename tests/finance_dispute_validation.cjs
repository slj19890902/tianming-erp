// Exercise the actual dispute handlers without a browser or formal database.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const html = fs.readFileSync('static/index.html', 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const extract = (start, end) => html.split(start)[1].split(end)[0].replace(/},\s*$/, '');
const handlers = [
  new AsyncFunction(extract('async saveStatementDispute() {', 'async openStatementEdit(row) {')),
  new AsyncFunction(extract('async reopenStatementDispute() {', 'async saveStatementDispute() {')),
  new AsyncFunction('item', extract('async openDisputeReceipt(item) {', 'async reopenStatementDispute() {')),
];

(async () => {
  for (const handler of handlers) {
    for (const reason of ['', '改', '  改  ', ' \n ', '改'.repeat(501)]) {
      let requests = 0, confirmations = 0;
      const messages = [];
      global.confirm = () => { confirmations++; return true; };
      global.axios = { post: async () => {
        requests++;
        throw { response: { data: { detail: [{loc: ['body', 'reason'], msg: 'String should have at least 2 characters'}] } } };
      } };
      const ctx = {
        statementDisputeState: { saving: false },
        statementDetail: { id: 1, version: 2, confirmation_status: 'confirmed', items: [
          { source_type: 'delivery', statement_item_id: 1, unit_price_snapshot: '10' },
        ] },
        statementDisputeForm: { reason, remove: {}, add: {}, edits: {1: {unit_price: '12.50'}} },
        showToast: message => messages.push(message),
        errorMessage: error => error.response.data.detail[0].msg,
        $refs: { statementDisputeReason: {focus() {}} },
      };
      assert.equal(await handler.call(ctx, {}), false);
      assert.equal(requests, 0, `Invalid reason ${JSON.stringify(reason)} must not write`);
      assert.equal(confirmations, 0, 'Invalid reason must be explained before confirming');
      assert.match(messages[0], /客户异议原因.*2.*500/);
      assert.equal(ctx.statementDisputeForm.edits[1].unit_price, '12.50');
      assert.equal(ctx.statementDetail.version, 2);
    }
  }
  console.log('PASS dispute reason validation blocks invalid save/reopen/receipt before any writes');
})().catch(error => { console.error(error); process.exitCode = 1; });

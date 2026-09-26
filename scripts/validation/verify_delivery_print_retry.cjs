const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const root = path.resolve(__dirname, '../..');
function setup(crypto) {
  const calls = [];
  const ctx = vm.createContext({ crypto, Uint8Array, Map, console,
    fetch: async (url, options) => { calls.push({url, body:JSON.parse(options.body)}); return {ok:true}; } });
  for (const file of ['operation-key.js', 'customer-delivery-print.js'])
    vm.runInContext(fs.readFileSync(path.join(root, 'static', file), 'utf8'), ctx);
  ctx.customerPrintData = {id:139, document_hash:'snapshot-a', price_display:{shown:false},
    order_context:'', print_template:{layout:{catalog_version:'delivery-print-v2'}}};
  return {ctx, calls};
}
(async () => {
  // LAN HTTP exposes getRandomValues but not randomUUID.
  const {ctx, calls} = setup({getRandomValues: a => webcrypto.getRandomValues(a)});
  await ctx.CustomerDeliveryPrint.recordPrint();
  assert.match(calls[0].body.idempotency_key, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  const originalFetch = ctx.fetch;
  ctx.fetch = async (...args) => { await originalFetch(...args); throw Error('response lost after commit'); };
  await assert.rejects(ctx.CustomerDeliveryPrint.recordPrint(), /response lost/);
  const lost = calls.at(-1).body;
  ctx.fetch = originalFetch;
  await ctx.CustomerDeliveryPrint.recordPrint();
  assert.deepEqual(calls.at(-1).body, lost);
  await ctx.CustomerDeliveryPrint.recordPrint();
  assert.notEqual(calls.at(-1).body.idempotency_key, lost.idempotency_key);
  ctx.fetch = async (...args) => { await originalFetch(...args); return {ok:false,status:503,json:async()=>({detail:'retry'})}; };
  await assert.rejects(ctx.CustomerDeliveryPrint.recordPrint(), /retry/);
  const failed = calls.at(-1).body;
  ctx.customerPrintData.price_display.shown = true;
  ctx.fetch = originalFetch;
  await ctx.CustomerDeliveryPrint.recordPrint();
  assert.notEqual(calls.at(-1).body.idempotency_key, failed.idempotency_key);
  ctx.customerPrintData.price_display.shown = false;
  await ctx.CustomerDeliveryPrint.recordPrint();
  assert.deepEqual(calls.at(-1).body, failed);
  let finish;
  ctx.fetch = (...args) => new Promise(resolve => { finish = async () => resolve(await originalFetch(...args)); });
  const first = ctx.CustomerDeliveryPrint.recordPrint();
  const second = ctx.CustomerDeliveryPrint.recordPrint();
  assert.equal(first, second);
  await finish(); await first;
  assert(calls.every(call => call.url === '/api/deliveries/139/customer-print-events'));
  const before = calls.length;
  ctx.customerPrintData.print_template.layout.catalog_version = 'delivery-print-v1';
  await ctx.CustomerDeliveryPrint.recordPrint();
  assert.equal(calls.length, before);
  const secure = setup({randomUUID:()=> 'secure-key'});
  await secure.ctx.CustomerDeliveryPrint.recordPrint();
  assert.equal(secure.calls[0].body.idempotency_key, 'secure-key');
  const unavailable = setup(undefined);
  assert.notEqual(unavailable.ctx.TmOperationKey.create(), unavailable.ctx.TmOperationKey.create());
  for (const file of ['delivery-print.html','delivery-print-designer.html']) {
    const html = fs.readFileSync(path.join(root,'static',file),'utf8');
    assert(html.includes('src="/operation-key.js"'));
    assert(!html.includes('()=>crypto.randomUUID()'));
  }
  console.log('PASS: LAN fallback, secure UUID, retry identity, changed price, concurrent clicks, legacy no-op, print-only endpoint');
})().catch(error => { console.error(error); process.exitCode=1; });

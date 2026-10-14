// Real recovery helper, including its JSON storage boundary. Invoked by HTTP tests.
const fs = require('node:fs');
require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const api = globalThis.TmDeliveryDispatchRecovery;
const memory = new Map();
const storage = {getItem: k => memory.get(k) ?? null, setItem: (k,v) => memory.set(k,v),
  key: i => [...memory.keys()][i], get length() { return memory.size; }};
const actor = input.body.expected_actor_id;
const controller = api.create(actor, storage);
const record = controller.prepare(input.body, input.display);
controller.sending(record.key);
controller.unknown(record.key);
if (input.closing) controller.closing(record.key);
const restored = api.create(actor, storage).get(record.key);
if (input.response) {
  const response = input.response;
  if (response.status) api.validateResolve(response, restored, actor);
  else if (response.result === 'completed') api.validateCompleted(response, restored, actor);
  else api.validateClosed(response, restored, actor);
  if ((response.status ?? response.result) === 'completed') controller.confirm(record.key, response.dispatch_receipt, response.current);
  if ((response.status ?? response.result) === 'closed') controller.close(record.key, response.closure_receipt, response.current);
}
process.stdout.write(JSON.stringify(controller.get(record.key)));

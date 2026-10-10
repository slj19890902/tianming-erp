const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.events = {}; this.attributes = {}; this.open = false; this.isConnected = true; this.textContent = ''; }
  append(...nodes) { nodes.forEach(node => { node.parent = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this.children.forEach(node => node.isConnected = false); this.children = []; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  async emit(name, event = {}) { for (const callback of this.events[name] || []) await callback(event); }
}
const find = (root, tag) => root.children.flatMap(child => [child, ...find(child, tag)]).filter(child => child.tagName === tag);
const tick = () => new Promise(setImmediate);
const calls = [], revoked = [], timers = new Map();
let response = () => Promise.resolve({ok: true, blob: async () => ({type: 'image/webp'})}), counter = 0;
const window = {};
const ctx = vm.createContext({window, document: {createElement: tag => new Element(tag)}, AbortController,
  fetch: (url, options) => { calls.push({url, options}); return response(url, options); },
  URL: {createObjectURL: () => 'blob:test-' + (++counter), revokeObjectURL: url => revoked.push(url)},
  setTimeout: callback => { const id = ++counter; timers.set(id, callback); return id; }, clearTimeout: id => timers.delete(id)});
vm.runInContext(fs.readFileSync('static/ui/mobile-product-drawings.js', 'utf8'), ctx);
const component = window.TmProductDrawings;
const product = (id = 1, kind = 'image') => ({product_id: id, drawings: {status: 'available', items: [{id: 'legacy', name: '<工程图纸>', kind,
  preview_url: `/api/mobile/erp/products/${id}/drawings/legacy/preview`, original_url: `/api/mobile/erp/products/${id}/drawings/legacy/original`}]}});
const container = new Element('section');
(async () => {
  const root = component.append(container, product());
  assert.equal(root.open, false); assert.equal(calls.length, 0, 'collapsed preview makes no request');
  root.open = true; await root.emit('toggle'); await tick();
  assert.equal(calls.length, 1); assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(find(root, 'img')[0].alt, '<工程图纸>');
  assert.equal(find(root, 'small')[0].textContent, '第 1 张', 'legacy attachment without uploaded_at has no upload claim');
  assert.equal(find(root, 'a')[0].href, '/api/mobile/erp/products/1/drawings/legacy/original');
  assert.equal(find(root, 'summary')[0].attributes['aria-expanded'], 'true');
  let stopped = false; await root.emit('click', {stopPropagation() { stopped = true; }}); assert.ok(stopped);
  root.open = false; await root.emit('toggle'); assert.equal(find(root, 'img').length, 0); assert.equal(revoked.length, 1);
  let resolveSlow;
  response = () => new Promise(resolve => { resolveSlow = resolve; });
  root.open = true; await root.emit('toggle'); await tick();
  component.disposeWithin(container);
  assert.ok(calls.at(-1).options.signal.aborted, 'dispose aborts an in-flight preview');
  const other = component.append(container, product(2, 'pdf'));
  response = () => Promise.resolve({ok: true, blob: async () => ({type: 'image/webp'})});
  other.open = true; await other.emit('toggle'); await tick();
  resolveSlow({ok: true, blob: async () => ({type: 'image/webp'})}); await tick();
  assert.equal(find(root, 'img').length, 0, 'late response does not resurrect previous product');
  assert.match(find(other, 'img')[0].alt, /PDF首页/);
  assert.match(find(other, 'a')[0].textContent, /完整 PDF/);
  component.disposeWithin(container);
  response = () => Promise.resolve({ok: false, status: 403});
  other.open = true; await other.emit('toggle'); await tick();
  assert.match(find(other, 'p')[0].textContent, /无图纸查看权限/);
  component.disposeWithin(container);
  response = () => Promise.resolve({ok: false, status: 422, json: async () => ({detail:'PDF图纸损坏、加密或页面过大，无法预览'})});
  other.open = true; await other.emit('toggle'); await tick();
  assert.match(find(other, 'p')[0].textContent, /损坏、加密/);
  component.disposeWithin(container);
  response = () => Promise.resolve({ok: false, status: 404});
  other.open = true; await other.emit('toggle'); await tick();
  assert.match(find(other, 'p')[0].textContent, /文件缺失/); assert.equal(find(other, 'button').length, 1);
  response = () => Promise.resolve({ok: true, blob: async () => ({type: 'image/webp'})});
  await find(other, 'button')[0].emit('click'); await tick(); assert.equal(find(other, 'img').length, 1);
  await find(other, 'img')[0].emit('error'); assert.match(find(other, 'p')[0].textContent, /无法显示/);
  component.disposeWithin(container);
  response = (_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(Object.assign(new Error(), {name:'AbortError'}))));
  other.open = true; await other.emit('toggle'); await tick();
  [...timers.values()].forEach(callback => callback()); await tick();
  assert.match(find(other, 'p')[0].textContent, /超时/); assert.equal(find(other, 'button').length, 1);
  component.disposeWithin(container);
  for (const status of ['none', 'forbidden']) {
    const state = component.append(container, {drawings: {status}}); assert.match(state.textContent, status === 'none' ? /无工程图纸/ : /权限/);
  }
  const unsafe = product(); unsafe.drawings.items[0].preview_url = 'https://other.test/secret';
  const malformed = component.append(container, unsafe); const before = calls.length;
  malformed.open = true; await malformed.emit('toggle'); await tick(); assert.equal(calls.length, before);
  assert.match(find(malformed, 'p')[0].textContent, /暂不可用/);
  component.disposeWithin(container);

  const multi = product(7);
  multi.drawings.items = Array.from({length: 5}, (_, index) => ({
    id: `drawing-${index + 1}`, name: `图纸 ${index + 1}`, kind: index === 4 ? 'pdf' : 'image',
    uploaded_at: `2026-10-0${9 - index}T10:00:00Z`,
    preview_url: `/api/mobile/erp/products/7/drawings/drawing-${index + 1}/preview`,
    original_url: `/api/mobile/erp/products/7/drawings/drawing-${index + 1}/original`
  }));
  const pending = new Map(), attempts = new Map();
  response = (url, options) => {
    const id = url.match(/drawing-(\d+)/)[1];
    attempts.set(id, (attempts.get(id) || 0) + 1);
    if (id === '2' && attempts.get(id) === 2) return Promise.resolve({ok: true, blob: async () => ({type: 'image/webp'})});
    return new Promise((resolve, reject) => {
      pending.set(id, resolve);
      options.signal.addEventListener('abort', () => reject(Object.assign(new Error(), {name: 'AbortError'})));
    });
  };
  const many = component.append(container, multi);
  assert.match(find(many, 'summary')[0].textContent, /工程图纸 · 5 张/);
  assert.equal(calls.filter(call => call.url.includes('/products/7/')).length, 0, 'collapsed list makes no requests');
  many.open = true; await many.emit('toggle');
  assert.equal(find(many, 'figure').length, 5, 'all attachments render immediately');
  assert.equal(find(many, 'a').length, 5, 'each original is available even while thumbnails load');
  assert.equal(calls.filter(call => call.url.includes('/products/7/')).length, 3, 'thumbnail requests have a concurrency limit');
  assert.match(find(many, 'small')[0].textContent, /最新上传 · 上传/);
  assert.doesNotMatch(find(many, 'small')[0].textContent, /现用版/);
  assert.match(find(many, 'small')[1].textContent, /上传/);
  pending.get('2')({ok: false, status: 404}); await tick();
  assert.equal(calls.filter(call => call.url.includes('/products/7/')).length, 4, 'one failed thumbnail advances the queue while the first is slow');
  assert.match(find(find(many, 'figure')[1], 'p')[0].textContent, /文件缺失/);
  await find(find(many, 'figure')[1], 'button')[0].emit('click');
  await find(find(many, 'figure')[1], 'button')[0].emit('click');
  assert.equal(calls.filter(call => call.url.includes('/products/7/')).length, 4, 'retry waits for its own bounded slot');
  pending.get('3')({ok: true, blob: async () => ({type: 'image/webp'})}); await tick();
  assert.equal(calls.filter(call => call.url.includes('/products/7/')).length, 5, 'later attachments keep loading');
  pending.get('4')({ok: true, blob: async () => ({type: 'image/webp'})}); await tick();
  assert.equal(attempts.get('2'), 2, 'retry fetches only the failed attachment');
  assert.equal(attempts.get('1'), 1);
  assert.match(find(many, 'a')[4].textContent, /完整 PDF/);
  const beforeCollapse = calls.length, beforeRevoke = revoked.length;
  many.open = false; await many.emit('toggle');
  assert.equal(find(many, 'figure').length, 0);
  assert.equal(calls.length, beforeCollapse, 'collapse stops the old queue');
  assert.ok(revoked.length > beforeRevoke, 'collapse releases loaded thumbnails');
  assert.ok(calls.filter(call => call.url.includes('/products/7/')).filter(call => call.options.signal.aborted).length >= 1);
  component.disposeWithin(container);
  const page = fs.readFileSync('static/mobile_erp.html', 'utf8');
  for (const match of page.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
  assert.match(page, /groupId !== "production"/);
  assert.match(page, /TmProductDrawings\?\.append\(header, product\)/);
  assert.match(page, /TmProductDrawings\?\.append\(row, line\)/);
  assert.match(page, /task\.drawing_path/);
  const live = fs.readFileSync('static/mobile_product_live.html', 'utf8');
  for (const match of live.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
  assert.match(live, /mobile-product-drawings\.js\?v=20261010-all/);
  assert.match(live, /mobile-product-drawings\.css\?v=20261010-all/);
  assert.match(live, /TmProductDrawings\?\.append\(\$\("productDrawings"\),row\)/);
  assert.match(live, /render\(data\)\{window\.TmProductDrawings\?\.disposeWithin\(\$\("content"\)\)/);
  assert.match(live, /pagehide",\(\)=>window\.TmProductDrawings\?\.disposeWithin\(\$\("content"\)\)/);
  assert.doesNotMatch(live, /row\.drawing_url\?/);
  console.log('Passed: all drawings, bounded previews, independent retry, upload time, lazy image/PDF preview, original links, permissions/empty, unsafe URL, collapse abort, stale response, weak-network timeout and mobile syntax');
})().catch(error => { console.error(error); process.exitCode = 1; });

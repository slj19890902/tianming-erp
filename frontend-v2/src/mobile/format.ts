/** Display aliases only. Database IDs and request identities always stay intact. */
export function shortCode(value: unknown): string {
  const raw = String(value ?? "").trim();
  if (!raw) return "—";
  const part =
    raw
      .split(/[.·/\s]+/)
      .filter(Boolean)
      .pop() || raw;
  const suffix = /^\d+$/.test(part) && part.length > 6 ? part.slice(-6) : part;
  return suffix.replace(/^0+(?=\d)/, "").slice(-12);
}
export function compactText(value: unknown): string {
  return String(value ?? "")
    .replace(/\b[A-Z]\.\d+\.\d+\b/gi, shortCode)
    .replace(/EDIT[-_]?0*(\d+)/gi, "E$1")
    .replace(/左侧第\s*(\d+)\s*位/g, "左$1")
    .replace(/右侧第\s*(\d+)\s*位/g, "右$1");
}
export function shortLocation(value: unknown): string {
  return (
    compactText(value)
      .replace(/[·｜|]/g, " ")
      .replace(/第\s*(\d+)\s*位/g, "$1位")
      .replace(/\s+/g, " ")
      .trim() || "—"
  );
}
export function quantity(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("zh-CN", { maximumFractionDigits: 3 })
    : "—";
}
export function money(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : "—";
}
export function dimensions(...values: unknown[]): string {
  return values.every((v) => v !== null && v !== undefined && v !== "")
    ? values.map(quantity).join("×")
    : "—";
}
export function safeMessage(error: unknown): string {
  return compactText(
    error instanceof Error ? error.message : "加载失败，请重试",
  );
}

/** Reuse the existing label route rules; QR origins are never used for navigation. */
export function mobileQrRoute(text: string, currentOrigin: string, apiOrigin: string): string | null {
  const current = new URL(currentOrigin), api = new URL(apiOrigin, current);
  const location = { origin: api.origin, hostname: api.hostname };
function shelfIdentity(text: string) {
  try {
    const url = new URL(text, location.origin);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    const hosts = new Set([location.hostname, current.hostname, 'tianmingerp0909.share.zrok.io', '192.168.3.80', '172.16.1.26']);
    if (!hosts.has(url.hostname)) return null;
    const match = url.pathname.match(/^\/q\/([1-9]\d*)(?:\/([a-f0-9]{24}))?\/?$/);
    if (match) return {id:match[1], product:match[2] || null};
    if (['/warehouse.html', '/static/shelf-scan.html'].includes(url.pathname)) {
      const target = url.searchParams.get('location_id'), key = url.searchParams.get('product');
      if (/^[1-9]\d*$/.test(target || '') && (!key || /^[a-f0-9]{24}$/.test(key))) return {id:target, product:key};
    }
  } catch (_) { /* A QR may contain text rather than an ERP URL. */ }
  return null;
}
function moldIdentity(text: string) {
  try {
    const url = new URL(text, location.origin);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    if (![location.hostname, current.hostname, 'tianmingerp0909.share.zrok.io', '192.168.3.80', '172.16.1.26'].includes(url.hostname)) return null;
    const match = url.pathname.match(/^\/M\/([1-9]\d*)\/?$/);
    if (match) {
      const task = url.searchParams.get('production_task_id');
      if (task && !/^[1-9]\d*$/.test(task)) return null;
      return `/M/${match[1]}${task ? '?production_task_id='+task : ''}`;
    }
    if (['/mobile/mold-lookup', '/static/mobile_mold_lookup.html'].includes(url.pathname)) {
      const moldId = url.searchParams.get('mold_id');
      if (/^[1-9]\d*$/.test(moldId || '')) return `/mobile/mold-lookup?mold_id=${moldId}&readonly=1`;
    }
  } catch (_) { /* Only recognized ERP identities are accepted. */ }
  return null;
}
function erpMobileRoute(text: string) {
  try {
    const url = new URL(text, location.origin);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    const hosts = new Set([location.hostname, current.hostname, 'tianmingerp0909.share.zrok.io', '192.168.3.80', '172.16.1.26']);
    if (!hosts.has(url.hostname)) return null;
    let match = url.pathname.match(/^\/P\/([1-9]\d*)\/?$/);
    if (match) return `/P/${match[1]}`;
    match = url.pathname.match(/^\/I\/([1-9]\d*)\/?$/);
    if (match) return `/I/${match[1]}`;
    match = url.pathname.match(/^\/sp\/([1-9]\d*)\/([1-9]\d*)\/([1-9]\d*)\/([1-9]\d*)\/?$/);
    if (match) return match[0];
    if (url.pathname === '/static/finished-goods-label.html') {
      const lotId = url.searchParams.get('lot_id');
      if (/^[1-9]\d*$/.test(lotId || '')) return `/I/${lotId}`;
    }
    if (url.pathname === '/scan/rack' || url.pathname === '/warehouse.html') {
      const floor = (url.searchParams.get('floor') || '').toUpperCase();
      const rackId = url.searchParams.get('rack_id') || '';
      if (/^\d{1,2}F$/.test(floor) && /^[A-Za-z0-9._:-]{1,80}$/.test(rackId)) {
        return `/scan/rack?floor=${encodeURIComponent(floor)}&rack_id=${encodeURIComponent(rackId)}`;
      }
    }
    if (url.pathname === '/incoming.html') return '/mobile/?mobile_page=incoming#incoming';
    if (['/mobile/', '/mobile/erp.html'].includes(url.pathname)) {
      const page = url.searchParams.get('mobile_page') || (url.searchParams.get('task_id') ? 'production' : 'home');
      if (!['home','lookup','incoming','warehouse','production','pre_delivery'].includes(page)) return null;
      const output = new URLSearchParams({mobile_page: page});
      for (const [name, pattern] of [
        ['task_id', /^[1-9]\d*$/], ['location_id', /^[1-9]\d*$/], ['lot_id', /^[1-9]\d*$/],
        ['floor_code', /^\d{1,2}F$/], ['rack_id', /^[A-Za-z0-9._:-]{1,80}$/], ['warehouse_map', /^1$/]
      ] as const) {
        const value = url.searchParams.get(name);
        if (value && pattern.test(value)) output.set(name, value);
      }
      return `/mobile/?${output.toString()}#${page}`;
    }
    if (url.pathname === '/mobile/delivery-pick.html') {
      const taskId = url.searchParams.get('task_id');
      return taskId && /^[1-9]\d*$/.test(taskId) ? `/mobile/delivery-pick.html?task_id=${taskId}` : '/mobile/delivery-pick.html';
    }
    if (url.pathname === '/mobile/tianhua-pick') {
      const token = url.searchParams.get('token') || '';
      if (/^[A-Za-z0-9._~-]{16,2048}$/.test(token)) return `/mobile/tianhua-pick?token=${encodeURIComponent(token)}`;
    }
  } catch (_) { /* Only recognized ERP identities are accepted. */ }
  return null;
}

  const shelf = shelfIdentity(text);
  if (shelf) return `/q/${shelf.id}${shelf.product ? `/${shelf.product}` : ''}`;
  return moldIdentity(text) || erpMobileRoute(text);
}

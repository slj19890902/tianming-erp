/** Navigation only: never retain quantities, credentials or write requests. */
const origins = new Set(["/mobile/query", "/mobile/recognize", "/mobile/warehouse", "/mobile/receive", "/mobile/more"]);
const keys = new Set(["q", "tab", "mode", "length", "width", "height", "tolerance", "page", "unfulfilled", "floor", "area", "location"]);
export function mobileReturn(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 1500 || !value.startsWith("/mobile/") || /[\\\r\n]/.test(value)) return null;
  const url = new URL(value, "https://mobile.invalid");
  if (url.origin !== "https://mobile.invalid" || !origins.has(url.pathname) || url.hash) return null;
  const query = new URLSearchParams();
  for (const [key, val] of url.searchParams) {
    if (keys.has(key) && val.length <= 100) query.set(key, val);
  }
  return url.pathname + (query.size ? `?${query}` : "");
}
export function mobileParent(path: string, from: unknown): string {
  if (/^\/mobile\/products\/[^/]+$/.test(path)) return mobileReturn(from) || "/mobile/query";
  if (path === "/mobile/recognize") return "/mobile/query?tab=board";
  return "/mobile";
}
export function mobileSection(path: string, from: unknown): string {
  const origin = path.startsWith("/mobile/products/") ? mobileReturn(from) || "/mobile/query" : path;
  if (origin.startsWith("/mobile/query") || origin.startsWith("/mobile/recognize")) return "/mobile/query";
  if (origin.startsWith("/mobile/warehouse")) return "/mobile/warehouse";
  if (origin === "/mobile") return "/mobile";
  return "/mobile/more";
}
export function mobilePage(value: unknown): number {
  const page = Number(value);
  return Number.isSafeInteger(page) && page >= 1 && page <= 100000 ? page : 1;
}

const WORKSPACE_PATHS = new Set(["/warehouse.html", "/warehouse-ledger.html"]);

export function normalizeWarehouseWorkspaceUrl(value, origin) {
  if (typeof value !== "string" || !value.trim()) return null;
  const base = new URL(origin || "http://localhost");
  let target;
  try {
    target = new URL(value, base);
  } catch {
    return null;
  }
  if (target.origin !== base.origin || !WORKSPACE_PATHS.has(target.pathname)) return null;
  return `${target.pathname}${target.search}${target.hash}`;
}

export function warehouseWorkspaceActivation(value, origin) {
  const url = normalizeWarehouseWorkspaceUrl(value, origin);
  if (!url) return null;
  const parsed = new URL(url, origin || "http://localhost");
  const numberParam = (name) => {
    if (!parsed.searchParams.has(name)) return undefined;
    const value = Number(parsed.searchParams.get(name));
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  };
  const textParam = (name) => parsed.searchParams.has(name)
    ? (parsed.searchParams.get(name) || "").trim()
    : undefined;
  return {
    url,
    pathname: parsed.pathname,
    q: textParam("q"),
    floor: textParam("floor"),
    locationId: numberParam("location_id"),
    lotId: numberParam("lot_id"),
    tab: textParam("tab"),
    action: textParam("action")
  };
}

export function warehouseWorkspaceBlockMessage(state) {
  if (state.moveSubmitting || state.stocktakeSubmitting || state.mergeSubmitting || state.otherSubmitting) {
    return "仓库操作正在提交，请等待当前结果后再切换页面。";
  }
  if (state.moveUncertain || state.stocktakeRefreshRequired || state.pendingRefreshRequired || state.rackOperationBlocked) {
    return state.rackOperationBlocked || "仓库操作结果尚未确认，请先用当前保留记录刷新核对。";
  }
  return "";
}

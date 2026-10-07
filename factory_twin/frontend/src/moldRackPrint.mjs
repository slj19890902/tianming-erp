export function moldRackPrintSelection(response, scope) {
  if (response?.floor_code !== scope.floorCode || response?.rack?.rack_id !== scope.rackId
      || response.truncated || response.total !== response.items?.length) {
    throw new Error("货架目录未完整读取，请刷新后重试");
  }
  if (scope.cellId && !response.rack.cells?.some(cell => cell.id === scope.cellId)) {
    throw new Error("模具格已变化，请刷新后重试");
  }
  const rows = response.items.filter(item => {
    if (item.is_active === false || item.archive_status === "archived") return false;
    if (scope.cellId) return String(item.location_guide?.cell_id || "").toLowerCase() === scope.cellId.toLowerCase();
    if (scope.level) {
      const guide = item.location_guide || {};
      const grid = Number(guide.grid || (["flat", "flat_legacy"].includes(guide.kind) ? guide.row : guide.kind === "vertical" ? 1 : 0));
      return Number(guide.level) === scope.level && grid === scope.grid;
    }
    return true;
  });
  const unique = [...new Map(rows.map(item => [item.id, item])).values()];
  unique.sort((a, b) => Number(a.location_guide?.level || 0) - Number(b.location_guide?.level || 0)
    || Number(a.location_guide?.grid || 0) - Number(b.location_guide?.grid || 0)
    || String(a.label_name || a.display_name || a.mold_name).localeCompare(String(b.label_name || b.display_name || b.mold_name), "zh-CN", {numeric: true})
    || a.id - b.id);
  if (!unique.length) throw new Error("当前范围没有可打印的模具");
  if (unique.length > 100) throw new Error(`当前范围有 ${unique.length} 块模具，单次最多100块，请按单格打印`);
  return unique;
}

export function createMoldRackPrinter({request, openWindow, confirmReprint, changed, makeKey}) {
  let attempt = null, busy = false;
  const update = message => changed({busy, pending: Boolean(attempt), message});
  async function run(scope, template = "mold_80x40_v1") {
    if (busy) return;
    if (attempt && JSON.stringify({scope, template}) !== attempt.signature) {
      update("请先继续上次打印，不能更换货架、格位或纸型"); return;
    }
    // Open during the direct click. A blocked popup never creates a print job.
    const popup = openWindow(attempt?.url || "about:blank");
    if (!popup) {update("请允许本站弹窗后重试"); return;}
    if (attempt?.url) {const count = attempt.payload.mold_ids.length; attempt = null; update(`已打开 ${count} 块在用模具标签预览`); return;}
    busy = true; update("正在准备模具标签…");
    try {
      if (!attempt) {
        const response = await request(`/api/warehouse/molds/by-map-rack?${new URLSearchParams({floor_code: scope.floorCode, rack_id: scope.rackId})}`);
        const items = moldRackPrintSelection(response, scope);
        const printed = items.filter(item => item.label_print_status?.printed).length;
        if (printed && !confirmReprint(`本次 ${items.length} 块模具中，${printed} 块已登记过标签打印，是否继续补打？`)) {
          popup.close(); busy = false; update(""); return;
        }
        attempt = {scope, template, signature: JSON.stringify({scope, template}),
          payload: {mold_ids: items.map(item => item.id), source: "batch", template_version: template, idempotency_key: makeKey()}};
        update("正在登记标签打印…");
      }
      const registered = await request("/api/warehouse/molds/label-prints", attempt.payload);
      if (!Number.isInteger(registered?.print_job_id) || registered.print_job_id <= 0) throw new Error("打印回执未确认");
      attempt.url = `/mold-label.html?mold_ids=${attempt.payload.mold_ids.join(",")}&template_version=${encodeURIComponent(attempt.template)}&print_job_id=${registered.print_job_id}`;
      if (popup.closed) {update("标签已登记，请继续打开预览"); return;}
      popup.location.href = attempt.url;
      const count = attempt.payload.mold_ids.length;
      attempt = null; update(`已打开 ${count} 块在用模具标签预览`);
    } catch (error) {
      popup.close();
      if (!attempt || (error.status && error.status < 500)) {attempt = null; update(error.message);}
      else update("打印结果未确认，请点“继续上次打印”核对，不会重复登记");
    } finally {busy = false; update(undefined);}
  }
  return {run, retry: () => attempt ? run(attempt.scope, attempt.template) : undefined};
}

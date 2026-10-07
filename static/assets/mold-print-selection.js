(function(global) {
"use strict";
function scopeRows(response, scope) {
  if (response?.floor_code !== scope.floorCode || response?.rack?.rack_id !== scope.rackId
      || response.truncated || response.total !== response.items?.length) {
    throw new Error("货架目录未完整读取，请刷新后重试");
  }
  if (scope.cellId && !response.rack.cells?.some(cell => cell.id === scope.cellId)) {
    throw new Error("模具格已变化，请刷新后重试");
  }
  const rows = response.items.filter(item => {
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
  return unique;
}


function defaultSelection(rows) {
  if (rows.some(row => typeof row.label_print_status?.printed !== "boolean")) throw new Error("打印状态读取不完整，请重新读取");
  return rows.filter(row => row.printable && !row.label_print_status.printed).map(row => row.id);
}
function createSubmission({request, navigate, confirmReprint, changed, makeKey, remember, savedAttempt=null}) {
  let attempt=savedAttempt, busy=false;
  const update=message=>changed({busy,pending:!!attempt,message});
  async function submit(rows, ids, template) {
    if (busy) return;
    if (!attempt) {
      if (!ids.length) {update("请选择要打印的模具");return;}
      if (ids.length>100) {update("单次最多100块，请减少勾选后分批打印");return;}
      const chosen=ids.map(id=>rows.find(row=>row.id===id));
      if (new Set(ids).size!==ids.length || chosen.some(row=>!row?.printable)) {update("所选模具资料已变化，请重新读取");return;}
      const repeated=chosen.filter(row=>row.label_print_status.printed).length;
      if (repeated&&!confirmReprint(`已选 ${ids.length} 块，其中 ${repeated} 块已打印。确认补打这些标签？`)) return;
      attempt={payload:{mold_ids:[...ids],source:"batch",template_version:template,idempotency_key:makeKey()}};
      remember(attempt);
    }
    busy=true;update("正在准备标签预览…");
    try {
      if (!attempt.url) {
        const registered=await request("/api/warehouse/molds/label-prints",attempt.payload);
        if (!Number.isInteger(registered?.print_job_id)||registered.print_job_id<=0) throw new Error("打印回执未确认");
        attempt.url=`/mold-label.html?mold_ids=${attempt.payload.mold_ids.join(",")}&template_version=${encodeURIComponent(attempt.payload.template_version)}&print_job_id=${registered.print_job_id}`;
        remember(attempt);
      }
      // Navigate in this already-open page; failures never close a window.
      navigate(attempt.url);
      remember(null);attempt=null;
    } catch(error) {
      if (!attempt?.url&&error.status&&error.status<500) {attempt=null;remember(null);update(error.message);}
      else update("打印结果未确认，请点“继续上次操作”核对，不会重复登记");
    } finally {busy=false;update(undefined);}
  }
  return {submit, state:()=>({busy,pending:!!attempt})};
}
global.TmMoldPrintSelection=Object.freeze({scopeRows,defaultSelection,createSubmission});
})(window);

export function createMoldRackPrinter({openWindow, changed}) {
  async function run(scope, template = "mold_80x40_v1") {
    const query = new URLSearchParams({floor_code: scope.floorCode, rack_id: scope.rackId, template_version: template});
    if (scope.cellId) query.set("cell_id", scope.cellId);
    else if (scope.level) {query.set("level", String(scope.level)); query.set("grid", String(scope.grid));}
    // No blank popup, background confirmation or print registration on entry.
    const popup = openWindow(`/static/mold-print-select.html?${query}`);
    changed({busy: false, pending: false, message: popup ? "" : "请允许本站弹窗后重试"});
  }
  return {run, retry: () => undefined};
}

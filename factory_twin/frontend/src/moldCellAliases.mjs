// Preview only. The server owns UUID allocation/restoration and validates history.
export function previewMoldCellAliases(rack) {
  const code = String(rack?.mold_rack_code || '').trim().toUpperCase();
  if (!/^[A-Z]{1,8}$/.test(code)) return {cells: [], errors: []};
  const counts = rack.level_cell_counts || [];
  const active = (rack.mold_cells || []).filter(c => c.level <= rack.levels && c.grid <= counts[c.level - 1]);
  const history = rack.retired_mold_cells || [];
  const errors = [];
  if (counts.some(count => count < 1)) errors.push('模具货架每层至少配置一格');
  const used = new Set();
  for (const cell of active) {
    const alias = String(cell.alias || '').trim().toUpperCase();
    if (!new RegExp(`^${code}[1-9]\\d*$`).test(alias)) errors.push(`第${cell.level}层第${cell.grid}格短号应为 ${code} 加正整数`);
    if (used.has(alias)) errors.push(`当前短号 ${alias} 重复，请改为不同编号`);
    used.add(alias);
  }
  const cells = [];
  let sequence = 1;
  for (let level = 1; level <= rack.levels; level++) {
    for (let grid = 1; grid <= (counts[level - 1] || 0); grid++) {
      const current = active.find(c => c.level === level && c.grid === grid);
      const slotHistory = history.filter(c => c.level === level && c.grid === grid);
      const candidates = new Set(slotHistory.map(c => c.id));
      const status = current ? 'current' : candidates.size === 1 ? 'restore' : candidates.size > 1 ? 'ambiguous' : 'new';
      let alias = current?.alias;
      if (!current) {
        while (used.has(`${code}${sequence}`)) sequence++;
        alias = `${code}${sequence}`;
        used.add(alias);
      }
      const reused = history.some(c => c.alias === String(alias).trim().toUpperCase() && c.id !== current?.id);
      const historyAliases = [...new Set(slotHistory.map(c => c.alias))];
      cells.push({id: current?.id || null, level, grid, alias, status, historyAliases, reused});
    }
  }
  return {cells, errors: [...new Set(errors)]};
}

function normalizedLevelCounts(rack) {
  const levels = Math.max(1, Math.round(Number(rack?.levels) || 1));
  const configured = Array.isArray(rack?.level_cell_counts)
    && rack.level_cell_counts.length === levels
    && rack.level_cell_counts.every((value) => Number.isInteger(value) && value >= 0 && value <= 50);
  if (configured) return rack.level_cell_counts.map((value) => Number(value));
  const bays = Math.max(1, Math.min(50, Math.round(Number(rack?.bays) || 1)));
  return Array.from({ length: levels }, () => bays);
}

function slotCoordinates(item) {
  const guide = item?.location_guide || {};
  const level = Number(guide.level);
  let grid = Number(guide.grid);
  if (!Number.isFinite(grid) || grid <= 0) {
    if (["flat", "flat_legacy"].includes(String(guide.kind || ""))) {
      grid = Number(guide.row);
    } else if (String(guide.kind || "") === "vertical") {
      grid = 1;
    } else {
      grid = 0;
    }
  }
  return {
    level: Number.isFinite(level) && level > 0 ? Math.round(level) : 0,
    grid: Number.isFinite(grid) && grid > 0 ? Math.round(grid) : 0
  };
}

export function buildMoldRackView(rack, items, blockedLevels = []) {
  const counts = normalizedLevelCounts(rack);
  const blocked = new Set((blockedLevels || []).map((value) => Number(value)));
  const levels = counts.map((cellCount, index) => ({
    level: index + 1,
    cell_count: cellCount,
    blocked: blocked.has(index + 1),
    cells: Array.from({ length: cellCount }, (_, cellIndex) => ({
      grid: cellIndex + 1,
      items: []
    })),
    level_only_items: []
  }));
  const rackOnlyItems = [];
  const unmatchedItems = [];

  for (const item of [...(items || [])].sort((left, right) => String(left.mold_code || "").localeCompare(String(right.mold_code || ""), "zh-CN", { numeric: true }))) {
    const { level, grid } = slotCoordinates(item);
    if (!level) {
      rackOnlyItems.push(item);
      continue;
    }
    const targetLevel = levels[level - 1];
    if (!targetLevel || targetLevel.blocked) {
      unmatchedItems.push(item);
      continue;
    }
    if (!grid) {
      targetLevel.level_only_items.push(item);
      continue;
    }
    const targetCell = targetLevel.cells[grid - 1];
    if (!targetCell) {
      unmatchedItems.push(item);
      continue;
    }
    targetCell.items.push(item);
  }

  return {
    levels,
    rack_only_items: rackOnlyItems,
    unmatched_items: unmatchedItems,
    total_items: (items || []).length
  };
}

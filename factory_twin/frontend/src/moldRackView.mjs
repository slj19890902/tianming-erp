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

function normalizedIdentity(value) {
  return String(value || "").trim().toUpperCase();
}

const MOLD_RACK_EMPLOYEE_NAMES = Object.freeze({
  R01: "左架",
  R02: "中架",
  R03: "右架"
});

export function moldRackEmployeeName(rack) {
  const code = normalizedIdentity(rack?.mold_rack_code || rack?.rack_code);
  const isMoldRack = Boolean(rack?.mold_rack_code)
    || /模具\s*00[12]/.test(String(rack?.name || ""))
    || normalizedIdentity(rack?.area_code).includes("MOLD");
  if (isMoldRack && MOLD_RACK_EMPLOYEE_NAMES[code]) {
    return MOLD_RACK_EMPLOYEE_NAMES[code];
  }
  return String(rack?.name || code || "货架").trim();
}

export function moldRacksForArea(feature, racks) {
  if (!feature) return [];
  const featureId = String(feature.id || "").trim();
  const areaIdentities = new Set([
    normalizedIdentity(feature.feature_code),
    normalizedIdentity(feature.erp_area_code)
  ].filter(Boolean));
  return (racks || []).filter((rack) => {
    if (!normalizedIdentity(rack?.mold_rack_code)) return false;
    if (featureId && String(rack?.area_feature_id || "").trim() === featureId) return true;
    return areaIdentities.has(normalizedIdentity(rack?.area_code));
  });
}

export function buildMoldShelfSpines(items) {
  const sortedItems = [...(items || [])].sort((left, right) =>
    String(left?.mold_code || "").localeCompare(String(right?.mold_code || ""), "zh-CN", { numeric: true })
  );
  return sortedItems.flatMap((item) => {
    const products = [...(item?.products || [])].sort((left, right) =>
      String(left?.product_code || left?.product_name || "").localeCompare(
        String(right?.product_code || right?.product_name || ""),
        "zh-CN",
        { numeric: true }
      ) || Number(left?.id || 0) - Number(right?.id || 0)
    );
    if (!products.length) {
      return [{
        key: `mold-${item.id}-unbound`,
        mold_id: item.id,
        product_id: null,
        code: item.mold_code || "未编号模具",
        name: `${item.mold_name || "未命名模具"}（未绑定产品）`,
        customer_name: null,
        mold_code: item.mold_code || "",
        mold_name: item.mold_name || "",
        rack_location: item.rack_location || ""
      }];
    }
    return products.map((product) => ({
      key: `mold-${item.id}-product-${product.id}`,
      mold_id: item.id,
      product_id: product.id,
      code: product.product_code || "无存货编码",
      name: product.product_name || "产品名称待补充",
      customer_name: product.customer_name || null,
      mold_code: item.mold_code || "",
      mold_name: item.mold_name || "",
      rack_location: item.rack_location || ""
    }));
  });
}

export function buildMoldLocationTarget(rack, levelValue, gridValue) {
  const rackCode = normalizedIdentity(rack?.rack_code);
  if (!/^R\d+$/.test(rackCode)) return null;
  const prefix = `1F-M-${rackCode}`;
  const levels = Array.isArray(rack?.levels) ? rack.levels : [];
  if (rack?.location_depth === "rack") return prefix;
  if (!levels.length) return null;

  const levelNumber = Math.round(Number(levelValue));
  const level = levels.find((item) => Number(item?.level) === levelNumber);
  if (!level) return null;
  const grids = Array.isArray(level.grids) ? level.grids.map((value) => Number(value)) : [];
  if (rack?.location_depth === "level") return `${prefix}-L${levelNumber}`;
  if (!grids.length) return null;

  const gridNumber = Math.round(Number(gridValue));
  if (!grids.includes(gridNumber)) return null;
  return `${prefix}-L${levelNumber}-G${String(gridNumber).padStart(2, "0")}`;
}

export function moldRackLevelUsage(items) {
  const usage = new Map();
  for (const item of items || []) {
    const { level } = slotCoordinates(item);
    if (level) usage.set(level, (usage.get(level) || 0) + 1);
  }
  return usage;
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

export function buildRackFrontSlots(rack) {
  const levels = Math.max(1, Math.round(Number(rack?.levels) || 1));
  const configuredCounts = Array.isArray(rack?.level_cell_counts) && rack.level_cell_counts.length === levels
    ? rack.level_cell_counts.map((value) => Math.min(50, Math.max(0, Math.round(Number(value) || 0))))
    : null;
  const legacyColumns = Math.min(5, Math.max(3, Math.round(Number(rack?.cargo_rows) || 3)));
  const heights = Array.isArray(rack?.level_heights_mm) ? rack.level_heights_mm : [];
  const rackHeight = Math.max(1, Number(rack?.height_mm) || 1);
  const rackCode = String(rack?.rack_code || "RACK");

  return Array.from({ length: levels }, (_, displayIndex) => {
    const tier = levels - displayIndex;
    const columns = configuredCounts ? configuredCounts[tier - 1] : legacyColumns;
    const isGround = tier === 1;
    const baseHeight = isGround ? 0 : Number(heights[tier - 2] ?? ((tier - 1) * rackHeight / levels));
    return {
      tier,
      isGround,
      title: isGround ? "地面栈板货物" : `${tier}层货架货物`,
      heightMm: Math.round(baseHeight),
      slots: Array.from({ length: columns }, (_, index) => ({
        id: `${rackCode}-L${tier}-${String(index + 1).padStart(2, "0")}`,
        tier,
        column: index + 1,
        isGround,
        label: isGround ? `栈板位 ${index + 1}` : `${tier}层 ${index + 1}位`,
        sampleSku: `纸箱货物-${String((tier * 7 + index * 3) % 19 + 1).padStart(2, "0")}`,
        quantity: 80 + ((tier * 41 + index * 67) % 240),
        unit: "只"
      }))
    };
  });
}

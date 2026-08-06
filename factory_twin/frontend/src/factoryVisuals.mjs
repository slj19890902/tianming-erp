const equipmentRules = [
  { kind: "printing", terms: ["印刷", "printer", "printing"] },
  { kind: "die_cutter", terms: ["模切", "die", "cutting"] },
  { kind: "forming", terms: ["粘箱", "钉箱", "成型", "glue", "stitch", "forming"] },
  { kind: "conveyor", terms: ["输送", "流水线", "conveyor"] }
];

export function classifyEquipment(name = "", category = "") {
  const searchable = `${name} ${category}`.toLowerCase();
  return equipmentRules.find((rule) => rule.terms.some((term) => searchable.includes(term)))?.kind || "generic";
}

export function distanceMm(first, second) {
  if (!first || !second) return 0;
  return Math.hypot(Number(second[0]) - Number(first[0]), Number(second[1]) - Number(first[1]));
}

export function formatDistanceMm(value) {
  const rounded = Math.round(Number(value) || 0);
  return rounded >= 1000 ? `${rounded.toLocaleString("zh-CN")} mm · ${(rounded / 1000).toFixed(2)} m` : `${rounded} mm`;
}

export function niceScaleLengthMm(targetMm) {
  const safeTarget = Math.max(Number(targetMm) || 1000, 1);
  const magnitude = 10 ** Math.floor(Math.log10(safeTarget));
  const normalized = safeTarget / magnitude;
  const factor = normalized >= 5 ? 5 : normalized >= 2 ? 2 : 1;
  return factor * magnitude;
}

export function accessDirectionVectors(accessSide) {
  const directions = {
    north: [[0, -1]],
    south: [[0, 1]],
    east: [[1, 0]],
    west: [[-1, 0]],
    both: [[0, -1], [0, 1]]
  };
  return directions[accessSide] || directions.south;
}

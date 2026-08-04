const kindPrefixes = {
  zone: "ZONE",
  aisle: "AISLE",
  no_go: "NO-GO"
};

const subtypePrefixes = {
  raw_material: "RAW",
  semi_finished: "SEMI",
  finished_wait_delivery: "FIN",
  mold: "MOLD",
  printing_plate: "PLATE",
  abnormal_isolation: "ABN",
  temporary_turnover: "TEMP",
  pedestrian: "PED",
  forklift: "FORK",
  fire: "FIRE",
  loading: "LOAD",
  fire_exit: "FIRE",
  electrical_box: "ELEC",
  maintenance: "MAINT",
  door_swing: "DOOR",
  freight_elevator: "LIFT"
};

function codePrefix(layout, kind, subtype) {
  const floor = String(layout?.floor_code || "1F").trim().toUpperCase();
  return `${kindPrefixes[kind]}-${floor}-${subtypePrefixes[subtype] || "AREA"}`;
}

export function nextFeatureCode(layout, kind, subtype, reservedCodes = []) {
  const prefix = codePrefix(layout, kind, subtype);
  const expression = new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}-(\\d+)$`, "i");
  const codes = [...(layout?.features || []).map((item) => item.feature_code), ...reservedCodes];
  const highest = codes.reduce((maximum, code) => {
    const match = String(code).trim().match(expression);
    return match ? Math.max(maximum, Number(match[1])) : maximum;
  }, 0);
  return `${prefix}-${String(highest + 1).padStart(3, "0")}`;
}

export function resolveFeatureCode(layout, kind, subtype, preferredCode, reservedCodes = []) {
  const preferred = String(preferredCode || "").trim().toUpperCase();
  const used = new Set([
    ...(layout?.features || []).map((item) => item.feature_code.toUpperCase()),
    ...reservedCodes.map((item) => String(item).toUpperCase())
  ]);
  if (preferred && !used.has(preferred)) return preferred;
  return nextFeatureCode(layout, kind, subtype, reservedCodes);
}

import { inventoryLocationItems, inventoryPhysicalQuantity } from "./warehouseInventory.mjs";

export const INTAKE_AGE_BANDS = Object.freeze([
  { bucket: "0_30", label: "0–30", color: "#DCF0E2", max: 30 },
  { bucket: "31_90", label: "31–90", color: "#AFCDB9", max: 90 },
  { bucket: "91_180", label: "91–180", color: "#D8C1BD", max: 180 },
  { bucket: "181_364", label: "181–364", color: "#C7837F", max: 364 },
  { bucket: "365_plus", label: "365+", color: "#A83F46", max: Infinity }
]);
export const UNKNOWN_INTAKE_COLOR = "#E4E7EB";

export function intakeAgePaint(ageDays) {
  if (typeof ageDays !== "number" || !Number.isFinite(ageDays) || ageDays < 0) {
    return { bucket: "unknown", color: UNKNOWN_INTAKE_COLOR, unknown: true, hasUnknown: true, empty: false };
  }
  const band = INTAKE_AGE_BANDS.find(row => Math.floor(ageDays) <= row.max);
  return { bucket: band.bucket, color: band.color, unknown: false, hasUnknown: false, empty: false };
}

export function oldestIntakePaint(items) {
  const physicalItems = (items || []).filter(item => inventoryPhysicalQuantity(item) > 0);
  if (!physicalItems.length) return { bucket: "empty", color: "#FFFFFF", unknown: false, empty: true };
  // Intake age is an authorized product projection. Never infer it from a lot's
  // stock_date, creation date, movement date or single-batch age_days.
  const knownItems = physicalItems.filter(item => !intakeAgePaint(item.intake_age_days).unknown);
  if (!knownItems.length) return intakeAgePaint(null);
  return { ...intakeAgePaint(Math.max(...knownItems.map(item => item.intake_age_days))), hasUnknown: knownItems.length !== physicalItems.length };
}

export function warehouseIntakePaints(locations, selectedItems = null) {
  const selectedIds = selectedItems === null ? null : new Set(selectedItems.map(item => item.lot_id));
  const allItems = locations.flatMap(inventoryLocationItems);
  const latestAges = new Map();
  for (const item of allItems) {
    if (!item.intake_identity_key || inventoryPhysicalQuantity(item) <= 0 || intakeAgePaint(item.intake_age_days).unknown) continue;
    latestAges.set(item.intake_identity_key, Math.min(latestAges.get(item.intake_identity_key) ?? Infinity, item.intake_age_days));
  }
  return Object.fromEntries(locations.map(location => {
    const items = inventoryLocationItems(location).filter(item => selectedIds === null || selectedIds.has(item.lot_id));
    const normalized = items.map(item => ({ ...item, intake_age_days: intakeAgePaint(item.intake_age_days).unknown
      ? null : latestAges.get(item.intake_identity_key) ?? item.intake_age_days }));
    const paint = oldestIntakePaint(normalized);
    return [location.location_id, { ...paint, dimmed: selectedIds !== null && paint.empty }];
  }));
}

export function rackIntakePaint(rackId, locations, paints) {
  const rows = locations.filter(location => location.map_rack_id === rackId)
    .map(location => paints[location.location_id]).filter(Boolean);
  const occupied = rows.filter(row => !row.empty);
  const knownRows = occupied.filter(row => !row.unknown);
  const paint = knownRows.length ? knownRows.reduce((oldest, row) => INTAKE_AGE_BANDS.findIndex(band => band.bucket === row.bucket)
      > INTAKE_AGE_BANDS.findIndex(band => band.bucket === oldest.bucket) ? row : oldest)
      : occupied.length ? intakeAgePaint(null) : { bucket: "empty", color: "#FFFFFF", unknown: false, empty: true };
  return { ...paint, hasUnknown: occupied.some(row => row.hasUnknown || row.unknown), dimmed: rows.length > 0 && rows.every(row => row.dimmed) };
}

export function intakeOutlineState({ selected = false, search = false, hover = false } = {}) {
  return selected ? { color: "#FACC15", width: 4 } : search ? { color: "#2563EB", width: 3 }
    : hover ? { color: "#475569", width: 3 } : { color: "#CBD5E1", width: 1 };
}

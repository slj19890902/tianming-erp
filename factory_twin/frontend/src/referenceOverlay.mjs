const LEGACY_BASE = Object.freeze({
  scale_x: 0.88795,
  scale_y: 1.09448,
  offset_x_mm: -15111,
  offset_y_mm: -11747
});

export function createDefaultReferenceOverlay(sharedCoordinates = false) {
  return {
    enabled: true,
    shared_coordinates: sharedCoordinates,
    offset_x_mm: 0,
    offset_y_mm: 0,
    scale_x: sharedCoordinates ? 1 : LEGACY_BASE.scale_x,
    scale_y: sharedCoordinates ? 1 : LEGACY_BASE.scale_y,
    mirror_x: sharedCoordinates ? false : true,
    mirror_y: sharedCoordinates ? false : true,
    rotation_deg: 0,
    opacity: 0.46
  };
}

export function normalizeReferenceOverlayDraft(saved, sharedCoordinates = false) {
  const defaults = createDefaultReferenceOverlay(sharedCoordinates);
  const raw = saved?.config;
  if (!raw || typeof raw !== "object") return defaults;
  if (sharedCoordinates && !raw.shared_coordinates) return defaults;
  if (Number(saved?.schemaVersion || 1) < 2) {
    return {
      ...defaults,
      ...raw,
      offset_x_mm: Number(raw.offset_x_mm || 0) - LEGACY_BASE.offset_x_mm,
      offset_y_mm: Number(raw.offset_y_mm || 0) - LEGACY_BASE.offset_y_mm,
      rotation_deg: 0
    };
  }
  return { ...defaults, ...raw };
}

export function referenceOverlayAnchor(bounds) {
  const middleX = (bounds.min_x + bounds.max_x) / 2;
  return {
    source_x_mm: (bounds.min_x + middleX) / 2,
    source_y_mm: (bounds.min_y + bounds.max_y) / 2
  };
}

export function transformReferencePoint(x, y, bounds, config) {
  const anchor = referenceOverlayAnchor(bounds);
  const baseTargetX = config.shared_coordinates ? anchor.source_x_mm : -anchor.source_x_mm * LEGACY_BASE.scale_x + LEGACY_BASE.offset_x_mm;
  const baseTargetY = config.shared_coordinates ? anchor.source_y_mm : -anchor.source_y_mm * LEGACY_BASE.scale_y + LEGACY_BASE.offset_y_mm;
  const scaledX = (config.mirror_x ? -1 : 1) * (x - anchor.source_x_mm) * config.scale_x;
  const scaledY = (config.mirror_y ? -1 : 1) * (y - anchor.source_y_mm) * config.scale_y;
  const angle = Number(config.rotation_deg || 0) * Math.PI / 180;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  const rotatedX = scaledX * cos - scaledY * sin;
  const rotatedY = scaledX * sin + scaledY * cos;
  return [
    baseTargetX + rotatedX + Number(config.offset_x_mm || 0),
    baseTargetY + rotatedY + Number(config.offset_y_mm || 0)
  ];
}

export { LEGACY_BASE };

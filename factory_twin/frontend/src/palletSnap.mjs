function effectiveSize(pallet) {
  return pallet.rotation_deg % 180 === 90
    ? { width: pallet.depth_mm, depth: pallet.width_mm }
    : { width: pallet.width_mm, depth: pallet.depth_mm };
}

export function palletRect(pallet, x = pallet.x_mm, y = pallet.y_mm) {
  const { width, depth } = effectiveSize(pallet);
  return { left: x - width / 2, right: x + width / 2, bottom: y - depth / 2, top: y + depth / 2 };
}

function overlaps(first, second) {
  return !(first.right <= second.left || first.left >= second.right || first.top <= second.bottom || first.bottom >= second.top);
}

function pointInPolygon([x, y], polygon) {
  let inside = false;
  for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index++) {
    const [x1, y1] = polygon[index];
    const [x2, y2] = polygon[previous];
    if ((y1 > y) !== (y2 > y) && x < ((x2 - x1) * (y - y1)) / (y2 - y1) + x1) inside = !inside;
  }
  return inside;
}

function fitsZone(rect, zones) {
  const points = [
    [rect.left + 0.01, rect.bottom + 0.01], [rect.right - 0.01, rect.bottom + 0.01],
    [rect.right - 0.01, rect.top - 0.01], [rect.left + 0.01, rect.top - 0.01],
    [(rect.left + rect.right) / 2, (rect.bottom + rect.top) / 2]
  ];
  return zones.some((zone) => zone.feature_kind === "zone" && zone.storage_mode === "floor" && points.every((point) => pointInPolygon(point, zone.points)));
}

export function snapPalletPosition(pallet, proposedX, proposedY, pallets, zones, threshold = 180, enabled = true) {
  if (!enabled || threshold <= 0) return { x: proposedX, y: proposedY, snapped: false, guides: [] };
  const { width, depth } = effectiveSize(pallet);
  const halfWidth = width / 2;
  const halfDepth = depth / 2;
  const xTargets = new Set();
  const yTargets = new Set();
  for (const zone of zones) {
    if (zone.feature_kind !== "zone" || zone.storage_mode !== "floor" || zone.points.length < 3) continue;
    const xs = zone.points.map((point) => point[0]);
    const ys = zone.points.map((point) => point[1]);
    const left = Math.min(...xs), right = Math.max(...xs), bottom = Math.min(...ys), top = Math.max(...ys);
    [left + halfWidth, (left + right) / 2, right - halfWidth].forEach((value) => xTargets.add(value));
    [bottom + halfDepth, (bottom + top) / 2, top - halfDepth].forEach((value) => yTargets.add(value));
  }
  for (const other of pallets) {
    if (other.id === pallet.id) continue;
    const rect = palletRect(other);
    [rect.left - halfWidth, rect.left + halfWidth, other.x_mm, rect.right - halfWidth, rect.right + halfWidth].forEach((value) => xTargets.add(value));
    [rect.bottom - halfDepth, rect.bottom + halfDepth, other.y_mm, rect.top - halfDepth, rect.top + halfDepth].forEach((value) => yTargets.add(value));
  }
  const nearbyX = [...xTargets].filter((value) => Math.abs(value - proposedX) <= threshold).sort((a, b) => Math.abs(a - proposedX) - Math.abs(b - proposedX)).slice(0, 8);
  const nearbyY = [...yTargets].filter((value) => Math.abs(value - proposedY) <= threshold).sort((a, b) => Math.abs(a - proposedY) - Math.abs(b - proposedY)).slice(0, 8);
  const candidates = [];
  for (const x of [proposedX, ...nearbyX]) for (const y of [proposedY, ...nearbyY]) {
    if (x === proposedX && y === proposedY) continue;
    candidates.push({ x, y, distance: Math.abs(x - proposedX) + Math.abs(y - proposedY) });
  }
  candidates.sort((a, b) => a.distance - b.distance);
  for (const candidate of candidates) {
    const rect = palletRect(pallet, candidate.x, candidate.y);
    if (!fitsZone(rect, zones)) continue;
    if (pallets.some((other) => other.id !== pallet.id && overlaps(rect, palletRect(other)))) continue;
    return {
      x: candidate.x,
      y: candidate.y,
      snapped: true,
      guides: [
        ...(candidate.x !== proposedX ? [{ axis: "x", value: candidate.x }] : []),
        ...(candidate.y !== proposedY ? [{ axis: "y", value: candidate.y }] : [])
      ]
    };
  }
  return { x: proposedX, y: proposedY, snapped: false, guides: [] };
}

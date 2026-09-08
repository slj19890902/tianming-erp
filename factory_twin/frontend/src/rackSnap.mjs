const EPS = .001;
function axes(rack) {
  // Canvas uses -rotation about world Y and warehouse Y = -world Z.
  const angle = -rack.rotation_deg * Math.PI / 180;
  return [[Math.cos(angle), Math.sin(angle)], [-Math.sin(angle), Math.cos(angle)]];
}
const dot = (a, b) => a[0] * b[0] + a[1] * b[1];
function overlaps(a, b) {
  const aa = axes(a), ba = axes(b), delta = [b.x_mm - a.x_mm, b.y_mm - a.y_mm];
  return [...aa, ...ba].every(axis => {
    const radius = (r, ax) => Math.abs(dot(axis, ax[0])) * r.width_mm / 2 + Math.abs(dot(axis, ax[1])) * r.depth_mm / 2;
    return Math.abs(dot(delta, axis)) < radius(a, aa) + radius(b, ba) - EPS;
  });
}

export function snapRackPosition(rack, x, y, racks, threshold = 120, enabled = true) {
  const raw = { x, y, snapped: false, guides: [] };
  if (!enabled || threshold <= 0) return raw;
  const basis = axes(rack), proposed = [dot([x, y], basis[0]), dot([x, y], basis[1])];
  const half = [rack.width_mm / 2, rack.depth_mm / 2];
  const others = racks.filter(other => other.id !== rack.id);
  const candidates = [];
  for (const other of others) {
    const quarter = (other.rotation_deg - rack.rotation_deg) / 90;
    if (Math.abs(quarter - Math.round(quarter)) > 1e-5) continue;
    const swapped = Math.abs(Math.round(quarter)) % 2 === 1;
    const oh = swapped ? [other.depth_mm / 2, other.width_mm / 2] : [other.width_mm / 2, other.depth_mm / 2];
    const center = basis.map(axis => dot([other.x_mm, other.y_mm], axis));
    for (const axis of [0, 1]) for (const sign of [-1, 1]) {
      const along = 1 - axis;
      const edge = center[axis] + sign * (half[axis] + oh[axis]);
      if (Math.abs(edge - proposed[axis]) > threshold) continue;
      // Require real side adjacency, not a distant corner or a rack across the floor.
      if (Math.abs(proposed[along] - center[along]) >= half[along] + oh[along] - EPS) continue;
      const alignments = [center[along] - oh[along] + half[along], center[along] + oh[along] - half[along], center[along]];
      const near = alignments.filter(value => Math.abs(value - proposed[along]) <= threshold)
        .sort((a, b) => Math.abs(a - proposed[along]) - Math.abs(b - proposed[along]));
      for (const aligned of [...near.slice(0, 1), proposed[along]]) {
        const local = [...proposed]; local[axis] = edge; local[along] = aligned;
        const nx = local[0] * basis[0][0] + local[1] * basis[1][0];
        const ny = local[0] * basis[0][1] + local[1] * basis[1][1];
        if (Math.hypot(nx - x, ny - y) > threshold * Math.SQRT2) continue;
        if (others.some(obstacle => overlaps({ ...rack, x_mm: nx, y_mm: ny }, obstacle))) continue;
        candidates.push({ x: nx, y: ny, snapped: true, guides: [], distance: Math.abs(edge - proposed[axis]), aligned: aligned !== proposed[along] });
      }
    }
  }
  candidates.sort((a, b) => a.distance - b.distance || Number(b.aligned) - Number(a.aligned));
  return candidates[0] || raw;
}

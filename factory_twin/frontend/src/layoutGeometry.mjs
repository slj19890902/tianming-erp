export function translatePointsMm(points, deltaXmm, deltaYmm) {
  return points.map(([x, y]) => [
    Math.round(x + deltaXmm),
    Math.round(y + deltaYmm)
  ]);
}

export function pointsBoundsMm(points) {
  if (!points.length) return { centerXmm: 0, centerYmm: 0, widthMm: 0, heightMm: 0 };
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  return {
    centerXmm: Math.round((minX + maxX) / 2),
    centerYmm: Math.round((minY + maxY) / 2),
    widthMm: Math.round(maxX - minX),
    heightMm: Math.round(maxY - minY)
  };
}

export function polygonAreaMm2(points) {
  if (!Array.isArray(points) || points.length < 3) return 0;
  let areaTwice = 0;
  for (let index = 0; index < points.length; index += 1) {
    const [x1, y1] = points[index];
    const [x2, y2] = points[(index + 1) % points.length];
    areaTwice += Number(x1) * Number(y2) - Number(x2) * Number(y1);
  }
  return Math.abs(areaTwice) / 2;
}

export function resizeAndMovePointsMm(points, centerXmm, centerYmm, widthMm, heightMm) {
  const bounds = pointsBoundsMm(points);
  const safeWidth = Math.max(1, Math.round(widthMm));
  const safeHeight = Math.max(1, Math.round(heightMm));
  return points.map(([x, y]) => {
    const xRatio = bounds.widthMm ? (x - bounds.centerXmm) / bounds.widthMm : 0;
    const yRatio = bounds.heightMm ? (y - bounds.centerYmm) / bounds.heightMm : 0;
    return [
      Math.round(centerXmm + xRatio * safeWidth),
      Math.round(centerYmm + yRatio * safeHeight)
    ];
  });
}

export function resizeSegmentMm(points, lengthMm) {
  if (points.length < 2) return points.map((point) => [...point]);
  const [start, end] = points;
  const centerX = (start[0] + end[0]) / 2;
  const centerY = (start[1] + end[1]) / 2;
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const currentLength = Math.hypot(dx, dy) || 1;
  const half = Math.max(1, lengthMm) / 2;
  const unitX = dx / currentLength;
  const unitY = dy / currentLength;
  return [
    [Math.round(centerX - unitX * half), Math.round(centerY - unitY * half)],
    [Math.round(centerX + unitX * half), Math.round(centerY + unitY * half)]
  ];
}

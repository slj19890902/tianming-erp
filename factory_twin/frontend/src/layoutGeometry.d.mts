export function translatePointsMm(
  points: number[][],
  deltaXmm: number,
  deltaYmm: number
): number[][];

export function pointsBoundsMm(points: number[][]): {
  centerXmm: number;
  centerYmm: number;
  widthMm: number;
  heightMm: number;
};

export function resizeAndMovePointsMm(
  points: number[][],
  centerXmm: number,
  centerYmm: number,
  widthMm: number,
  heightMm: number
): number[][];

export function resizeSegmentMm(points: number[][], lengthMm: number): number[][];

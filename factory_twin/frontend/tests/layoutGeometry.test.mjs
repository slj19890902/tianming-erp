import assert from "node:assert/strict";
import test from "node:test";

import {
  pointsBoundsMm,
  resizeAndMovePointsMm,
  resizeSegmentMm,
  translatePointsMm
} from "../src/layoutGeometry.mjs";

test("dragging a semantic feature translates every point without changing its shape", () => {
  const source = [[100, 200], [1100, 200], [1100, 700], [100, 700]];
  const translated = translatePointsMm(source, 325.4, -80.6);

  assert.deepEqual(translated, [[425, 119], [1425, 119], [1425, 619], [425, 619]]);
  assert.deepEqual(source, [[100, 200], [1100, 200], [1100, 700], [100, 700]]);
  assert.equal(translated[1][0] - translated[0][0], 1000);
  assert.equal(translated[2][1] - translated[1][1], 500);
});

test("the same translation works for an aisle polyline", () => {
  assert.deepEqual(
    translatePointsMm([[0, 0], [4000, 0], [4000, 6000]], -500, 750),
    [[-500, 750], [3500, 750], [3500, 6750]]
  );
});

test("a rectangular zone can be moved and resized with real millimetre dimensions", () => {
  const source = [[0, 0], [4000, 0], [4000, 2000], [0, 2000]];
  assert.deepEqual(pointsBoundsMm(source), {
    centerXmm: 2000, centerYmm: 1000, widthMm: 4000, heightMm: 2000
  });
  const resized = resizeAndMovePointsMm(source, 10000, -5000, 6000, 3000);
  assert.deepEqual(resized, [[7000, -6500], [13000, -6500], [13000, -3500], [7000, -3500]]);
  assert.deepEqual(pointsBoundsMm(resized), {
    centerXmm: 10000, centerYmm: -5000, widthMm: 6000, heightMm: 3000
  });
});

test("door window and column segments resize around their centre without changing direction", () => {
  assert.deepEqual(resizeSegmentMm([[1000, 2000], [3000, 2000]], 4000), [[0, 2000], [4000, 2000]]);
  assert.deepEqual(resizeSegmentMm([[2000, 1000], [2000, 3000]], 1000), [[2000, 1500], [2000, 2500]]);
});

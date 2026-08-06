import test from "node:test";
import assert from "node:assert/strict";

import { snapPalletPosition } from "../src/palletSnap.mjs";

const zone = {
  feature_kind: "zone",
  storage_mode: "floor",
  points: [[0, 0], [5000, 0], [5000, 5000], [0, 5000]]
};
const first = {
  id: "PAL-001", x_mm: 1000, y_mm: 1000,
  width_mm: 1200, depth_mm: 1000, rotation_deg: 0
};
const moving = {
  id: "PAL-002", x_mm: 2140, y_mm: 1000,
  width_mm: 1200, depth_mm: 1000, rotation_deg: 0
};

test("nearby pallet snaps edge-to-edge and emits alignment guides", () => {
  const result = snapPalletPosition(moving, 2140, 1000, [first, moving], [zone], 180, true);
  assert.equal(result.snapped, true);
  assert.deepEqual([result.x, result.y], [2200, 1000]);
  assert.ok(result.guides.some((guide) => guide.axis === "x"));
});

test("distant pallet stays at free coordinates", () => {
  const result = snapPalletPosition(moving, 4000, 2200, [first, moving], [zone], 180, true);
  assert.equal(result.snapped, false);
  assert.deepEqual([result.x, result.y], [4000, 2200]);
  assert.deepEqual(result.guides, []);
});

test("disabled snapping preserves nearby coordinates", () => {
  const result = snapPalletPosition(moving, 2140, 1000, [first, moving], [zone], 180, false);
  assert.equal(result.snapped, false);
  assert.deepEqual([result.x, result.y], [2140, 1000]);
});

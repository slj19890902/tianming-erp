import test from "node:test";
import assert from "node:assert/strict";
import { snapRackPosition } from "../src/rackSnap.mjs";
const rack = (id, x = 0, y = 0, rotation = 0) => ({ id, x_mm: x, y_mm: y, width_mm: 2000, depth_mm: 1000, rotation_deg: rotation });
test("nearby racks snap flush and align ends without changing angle", () => {
  const a = rack("a"), b = rack("b", 4000);
  const snap = snapRackPosition(a, 1920, 40, [a, b]);
  assert.equal(snap.snapped, true); assert.equal(snap.x, 2000); assert.equal(snap.y, 0); assert.equal(a.rotation_deg, 0);
});
test("remote racks, disabled snapping and self are ignored", () => {
  const a = rack("a"), b = rack("b", 4000);
  assert.equal(snapRackPosition(a, 1800, 0, [a, b]).snapped, false);
  assert.equal(snapRackPosition(a, 1920, 4000, [a, b]).snapped, false);
  assert.equal(snapRackPosition(a, 1920, 0, [a, b], 120, false).x, 1920);
  assert.equal(snapRackPosition(a, 10, 0, [a]).snapped, false);
});
test("quarter-turn dimensions are swapped; opposite orientation also works", () => {
  for (const angle of [90, 270]) { const result = snapRackPosition(rack("a"), 2420, 0, [rack("b", 4000, 0, angle)]);
    assert.equal(result.snapped, true); assert.equal(result.x, 2500); }
  assert.equal(snapRackPosition(rack("a"), 1920, 0, [rack("b", 4000, 0, 180)]).x, 2000);
});
test("parallel arbitrary angle works in the same warehouse coordinate system as Canvas", () => {
  const c = Math.cos(Math.PI / 6), s = -Math.sin(Math.PI / 6);
  const a = rack("a", 0, 0, 30), b = rack("b", 4000 * c, 4000 * s, 30);
  const result = snapRackPosition(a, 1920 * c, 1920 * s, [a, b]);
  assert.equal(result.snapped, true); assert.ok(Math.abs(result.x - 2000 * c) < .001); assert.ok(Math.abs(result.y - 2000 * s) < .001);
});
test("obstacles prevent snapping into an overlap", () => {
  const obstacle = { ...rack("obstacle", 1900, 0), width_mm: 100, depth_mm: 100 };
  assert.equal(snapRackPosition(rack("a"), 1920, 0, [rack("b", 4000), obstacle]).snapped, false);
});
test("nonparallel racks do not rotate to force a snap", () => {
  assert.equal(snapRackPosition(rack("a"), 1920, 0, [rack("b", 4000, 0, 35)]).snapped, false);
});

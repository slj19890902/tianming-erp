import test from "node:test";
import assert from "node:assert/strict";
import {
  createDefaultReferenceOverlay,
  LEGACY_BASE,
  normalizeReferenceOverlayDraft,
  transformReferencePoint
} from "../src/referenceOverlay.mjs";

const bounds = { min_x: -24000, min_y: -30000, max_x: 35000, max_y: 15000 };

test("manual X and Y offsets translate the overlay by the exact millimetre values", () => {
  const base = createDefaultReferenceOverlay();
  const point = transformReferencePoint(-12000, -8000, bounds, base);
  const moved = transformReferencePoint(-12000, -8000, bounds, {
    ...base,
    offset_x_mm: 5000,
    offset_y_mm: -3200
  });
  assert.equal(Math.round(moved[0] - point[0]), 5000);
  assert.equal(Math.round(moved[1] - point[1]), -3200);
});

test("reference overlay rotates around its fitted left-half anchor", () => {
  const base = createDefaultReferenceOverlay();
  const anchorPoint = transformReferencePoint(-9250, -7500, bounds, base);
  const rotatedAnchor = transformReferencePoint(-9250, -7500, bounds, { ...base, rotation_deg: 90 });
  assert.deepEqual(rotatedAnchor.map(Math.round), anchorPoint.map(Math.round));
  const point = transformReferencePoint(-8250, -7500, bounds, base);
  const rotated = transformReferencePoint(-8250, -7500, bounds, { ...base, rotation_deg: 90 });
  assert.equal(Math.round(Math.hypot(point[0] - anchorPoint[0], point[1] - anchorPoint[1])), Math.round(Math.hypot(rotated[0] - anchorPoint[0], rotated[1] - anchorPoint[1])));
});

test("legacy absolute-intercept drafts migrate without changing their displayed position", () => {
  const migrated = normalizeReferenceOverlayDraft({ schemaVersion: 1, config: { ...createDefaultReferenceOverlay(), offset_x_mm: LEGACY_BASE.offset_x_mm, offset_y_mm: LEGACY_BASE.offset_y_mm } });
  assert.equal(migrated.offset_x_mm, 0);
  assert.equal(migrated.offset_y_mm, 0);
  assert.equal(migrated.rotation_deg, 0);
});

test("aligned 1F uses the same coordinate frame as 3F and ignores the obsolete fitted draft", () => {
  const shared = createDefaultReferenceOverlay(true);
  assert.equal(shared.shared_coordinates, true);
  assert.equal(shared.mirror_x, false);
  assert.equal(shared.mirror_y, false);
  assert.deepEqual(transformReferencePoint(-12000, -8000, bounds, shared).map(Math.round), [-12000, -8000]);
  const obsolete = { schemaVersion: 2, config: { ...createDefaultReferenceOverlay(), offset_x_mm: 11500, offset_y_mm: 5100, rotation_deg: -90 } };
  assert.deepEqual(normalizeReferenceOverlayDraft(obsolete, true), shared);
});

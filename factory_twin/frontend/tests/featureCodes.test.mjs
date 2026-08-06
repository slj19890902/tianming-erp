import test from "node:test";
import assert from "node:assert/strict";

import { nextFeatureCode, resolveFeatureCode } from "../src/featureCodes.mjs";

const layout = {
  floor_code: "1F",
  features: [
    { feature_code: "ZONE-1F-RAW-001" },
    { feature_code: "ZONE-1F-RAW-003" },
    { feature_code: "AISLE-1F-FORK-001" }
  ]
};

test("repeated drawing receives the next available code", () => {
  assert.equal(nextFeatureCode(layout, "zone", "raw_material"), "ZONE-1F-RAW-004");
  assert.equal(resolveFeatureCode(layout, "zone", "raw_material", "ZONE-1F-RAW-001"), "ZONE-1F-RAW-004");
});

test("a unique operator code is preserved", () => {
  assert.equal(resolveFeatureCode(layout, "zone", "raw_material", "CUSTOM-AREA-08"), "CUSTOM-AREA-08");
});

test("each semantic type has a stable floor-aware prefix", () => {
  assert.equal(nextFeatureCode(layout, "zone", "delivery_surplus"), "ZONE-1F-SURPLUS-001");
  assert.equal(nextFeatureCode(layout, "zone", "rack_storage"), "ZONE-1F-RACK-001");
  assert.equal(nextFeatureCode(layout, "zone", "finished_storage"), "ZONE-1F-FG-001");
  assert.equal(nextFeatureCode(layout, "aisle", "shared_main"), "AISLE-1F-MAIN-001");
  assert.equal(nextFeatureCode(layout, "aisle", "fire"), "AISLE-1F-FIRE-001");
  assert.equal(nextFeatureCode(layout, "no_go", "door_swing"), "NO-GO-1F-DOOR-001");
  assert.equal(nextFeatureCode(layout, "structure", "rolling_door"), "STRUCT-1F-DOOR-001");
  assert.equal(nextFeatureCode(layout, "structure", "custom_column"), "COL-1F-001");
  assert.equal(nextFeatureCode(layout, "structure", "freight_elevator"), "LIFT-001");
});

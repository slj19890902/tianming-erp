import assert from "node:assert/strict";
import test from "node:test";

import {
  aisleSurfaceStyle,
  filterOperationalFeatures,
  operationalEntitySelectable,
  warehouseAisleColor,
  warehouseFrustumDivisor,
  wallSurfaceStyle
} from "../src/operationalView.mjs";

const bounds = { min_x: -24500, min_y: -29700, max_x: 1150, max_y: 4200 };
const insideColumn = {
  id: "inside",
  feature_kind: "structure",
  subtype: "custom_column",
  points: [[-2389, -12384], [-1739, -12384]]
};
const projectedSouthColumn = {
  id: "outside",
  feature_kind: "structure",
  subtype: "custom_column",
  points: [[8170, -12384], [8820, -12384]]
};
const outsideWall = {
  id: "wall",
  feature_kind: "structure",
  subtype: "custom_wall",
  points: [[8170, -12384], [12000, -12384]]
};
const measuredDispatchZone = {
  id: "measured-dispatch",
  feature_kind: "zone",
  subtype: "finished_wait_delivery",
  points: [[-1350, -13730], [1100, -13730], [1100, -7980], [-1350, -7980]]
};
const projectedOutdoorZone = {
  id: "projected-outdoor",
  feature_kind: "zone",
  subtype: "finished_wait_delivery",
  points: [[13704, -1903], [17704, -1903], [17704, 9089], [13704, 9089]]
};

test("1F operational view keeps measured zones and hides projected zones outside the envelope", () => {
  const result = filterOperationalFeatures("1F", bounds, [insideColumn, projectedSouthColumn, outsideWall, measuredDispatchZone, projectedOutdoorZone]);
  assert.deepEqual(result.map((item) => item.id), ["inside", "wall", "measured-dispatch"]);
});

test("3F operational view preserves the measured feature set", () => {
  const features = [insideColumn, projectedSouthColumn, outsideWall];
  assert.deepEqual(filterOperationalFeatures("3F", bounds, features), features);
});

test("warehouse framing fills 1F while preserving the accepted 3F global fit", () => {
  assert.equal(warehouseFrustumDivisor("1F", "warehouse"), 2.65);
  assert.equal(warehouseFrustumDivisor("3F", "warehouse"), 2.65);
  assert.equal(warehouseFrustumDivisor("1F", "editor"), 1.8);
});

test("warehouse aisles use a pale green surface and reserve strong green for empty locations", () => {
  assert.equal(warehouseAisleColor("1F", "warehouse", "#22c55e"), "#dcefe3");
  assert.equal(warehouseAisleColor("3F", "warehouse", "#f59e0b"), "#dcefe3");
  assert.equal(warehouseAisleColor("1F", "editor", "#22c55e"), "#22c55e");
});

test("warehouse aisle intersections use one opaque depth-writing surface", () => {
  assert.deepEqual(aisleSurfaceStyle("warehouse"), {
    transparent: false,
    opacity: 1,
    depthWrite: true,
    heightMm: 16,
    elevationMm: 10
  });
});

test("warehouse interaction whitelist includes zones, racks and production equipment", () => {
  assert.equal(operationalEntitySelectable("warehouse", "feature", "zone"), true);
  assert.equal(operationalEntitySelectable("warehouse", "equipment"), true);
  assert.equal(operationalEntitySelectable("warehouse", "feature", "aisle"), false);
  assert.equal(operationalEntitySelectable("warehouse", "feature", "structure"), false);
  assert.equal(operationalEntitySelectable("warehouse", "structure"), false);
  assert.equal(operationalEntitySelectable("warehouse", "rack"), true);
  assert.equal(operationalEntitySelectable("warehouse", "pallet"), false);
  assert.equal(operationalEntitySelectable("editor", "structure"), true);
  assert.equal(operationalEntitySelectable("editor", "rack"), true);
});

test("warehouse walls remain visible without occluding zones", () => {
  assert.deepEqual(wallSurfaceStyle("warehouse", "25d"), {
    transparent: true,
    opacity: 0.34,
    depthWrite: false
  });
  assert.deepEqual(wallSurfaceStyle("warehouse", "2d"), {
    transparent: true,
    opacity: 0.2,
    depthWrite: false
  });
  assert.deepEqual(wallSurfaceStyle("editor", "25d"), {
    transparent: true,
    opacity: 0.78,
    depthWrite: true
  });
});

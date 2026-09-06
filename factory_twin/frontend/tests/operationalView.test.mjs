import assert from "node:assert/strict";
import test from "node:test";

import {
  aisleSurfaceStyle,
  effectiveMapFeatures,
  filterOperationalFeatures,
  operationalEntitySelectable,
  warehouseAisleColor,
  warehouseFrustumDivisor,
  warehousePassageEnvelope,
  warehousePassageSurfaceStyle,
  warehouseZoneColor,
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

test("warehouse zones use one color across historical left and right area sources", () => {
  assert.equal(warehouseZoneColor("warehouse", "#eab308"), "#60a5fa");
  assert.equal(warehouseZoneColor("warehouse", "#8b5cf6"), "#60a5fa");
  assert.equal(warehouseZoneColor("editor", "#eab308"), "#eab308");
});

test("warehouse passage follows the wall envelope instead of the rectangular import bounds", () => {
  const wall = (points) => ({ kind: "wall", geometry: { type: "polyline", closed: true, points } });
  const envelope = warehousePassageEnvelope(
    { min_x: 0, min_y: 0, max_x: 10000, max_y: 10000 },
    [
      wall([[1000, 0], [1200, 0], [1200, 10000], [1000, 10000]]),
      wall([[8000, 0], [8200, 0], [8200, 6000], [8000, 6000]]),
      wall([[6000, 6000], [6200, 6000], [6200, 10000], [6000, 10000]])
    ],
    20
  );
  assert.ok(envelope.length >= 8);
  const lower = envelope.filter(([, y]) => y < 5000).map(([x]) => x);
  const upper = envelope.filter(([, y]) => y > 7000).map(([x]) => x);
  assert.ok(Math.max(...lower) >= 8000);
  assert.ok(Math.max(...upper) <= 6200);
  assert.ok(Math.min(...envelope.map(([x]) => x)) >= 1000);
});

test("warehouse passage bridges short exterior wall gaps without following an inside wall", () => {
  const wall = (points) => ({ kind: "wall", geometry: { type: "polyline", closed: true, points } });
  const envelope = warehousePassageEnvelope(
    { min_x: 0, min_y: 0, max_x: 10000, max_y: 10000 },
    [
      wall([[1000, 0], [1200, 0], [1200, 4000], [1000, 4000]]),
      wall([[1000, 5200], [1200, 5200], [1200, 10000], [1000, 10000]]),
      wall([[4000, 0], [4200, 0], [4200, 10000], [4000, 10000]]),
      wall([[8800, 0], [9000, 0], [9000, 10000], [8800, 10000]])
    ],
    40
  );
  const leftHalf = envelope.slice(0, envelope.length / 2);
  const gapRows = leftHalf.filter(([, y]) => y >= 4000 && y <= 5200);
  assert.ok(gapRows.length >= 4);
  assert.ok(Math.max(...gapRows.map(([x]) => x)) <= 1200);
});

test("warehouse uses the floor envelope as passage and hides legacy drawn aisles", () => {
  const aisle = { id: "legacy-aisle", feature_kind: "aisle", points: [[0, 0], [1000, 0]] };
  const zone = { id: "zone", feature_kind: "zone", points: [[0, 0], [1000, 0], [1000, 1000], [0, 1000]] };
  assert.deepEqual(warehousePassageSurfaceStyle("warehouse"), {
    visible: true,
    color: "#dcefe3",
    elevationMm: 0
  });
  assert.deepEqual(effectiveMapFeatures("warehouse", [aisle, zone]), [zone]);
  assert.deepEqual(effectiveMapFeatures("editor", [aisle, zone]), [aisle, zone]);
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

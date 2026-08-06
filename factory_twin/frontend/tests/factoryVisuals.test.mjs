import test from "node:test";
import assert from "node:assert/strict";

import {
  accessDirectionVectors,
  classifyEquipment,
  distanceMm,
  formatDistanceMm,
  niceScaleLengthMm
} from "../src/factoryVisuals.mjs";

test("equipment names select stable parametric visual archetypes", () => {
  assert.equal(classifyEquipment("2号模切机", "生产设备"), "die_cutter");
  assert.equal(classifyEquipment("水性印刷机", "印刷"), "printing");
  assert.equal(classifyEquipment("半自动粘箱机", "成型"), "forming");
  assert.equal(classifyEquipment("临时设备", "其他"), "generic");
});

test("measurement remains millimetre based while showing metres", () => {
  assert.equal(distanceMm([0, 0], [3000, 4000]), 5000);
  assert.equal(formatDistanceMm(5000), "5,000 mm · 5.00 m");
  assert.equal(formatDistanceMm(850), "850 mm");
});

test("scale bar and rack access directions are deterministic", () => {
  assert.equal(niceScaleLengthMm(3700), 2000);
  assert.deepEqual(accessDirectionVectors("both"), [[0, -1], [0, 1]]);
  assert.deepEqual(accessDirectionVectors("east"), [[1, 0]]);
});

import assert from "node:assert/strict";
import test from "node:test";
import { buildRackFrontSlots } from "../src/rackFront.mjs";

test("three-level rack renders upper shelves and a ground pallet row", () => {
  const tiers = buildRackFrontSlots({
    rack_code: "RACK-3F-F1-01", levels: 3, cargo_rows: 4,
    height_mm: 2200, level_heights_mm: [750, 1500]
  });
  assert.equal(tiers.length, 3);
  assert.deepEqual(tiers.map((tier) => tier.tier), [3, 2, 1]);
  assert.deepEqual(tiers.map((tier) => tier.title), ["3层货架货物", "2层货架货物", "地面栈板货物"]);
  assert.deepEqual(tiers.map((tier) => tier.heightMm), [1500, 750, 0]);
  assert.equal(tiers.flatMap((tier) => tier.slots).length, 12);
  assert.equal(tiers.at(-1).slots[0].isGround, true);
});

test("cargo columns stay within the manually allowed 3 to 5 rows", () => {
  assert.equal(buildRackFrontSlots({ levels: 1, cargo_rows: 1 })[0].slots.length, 3);
  assert.equal(buildRackFrontSlots({ levels: 1, cargo_rows: 9 })[0].slots.length, 5);
});

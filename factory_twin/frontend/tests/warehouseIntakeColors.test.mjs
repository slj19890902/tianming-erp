import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { intakeAgePaint, oldestIntakePaint, warehouseIntakePaints, rackIntakePaint, intakeOutlineState } from "../src/warehouseIntakeColors.mjs";

const item = (lot_id, intake_age_days, intake_identity_key = `product:${lot_id}`, quantity = 10) => ({ lot_id, intake_age_days, intake_identity_key, quantity });
const location = (location_id, items, map_rack_id = "A1") => ({ location_id, map_rack_id, loose_items: items, pallet: null });

test("five intake bands include all exact boundaries", () => {
  const expected = [[0, "#DCF0E2"], [30, "#DCF0E2"], [31, "#AFCDB9"], [90, "#AFCDB9"], [91, "#D8C1BD"], [180, "#D8C1BD"], [181, "#C7837F"], [364, "#C7837F"], [365, "#A83F46"], [800, "#A83F46"]];
  for (const [age, color] of expected) assert.equal(intakeAgePaint(age).color, color);
  for (const age of [undefined, null, NaN, Infinity, -1, "10"]) assert.equal(intakeAgePaint(age).bucket, "unknown");
});

test("unknown dates do not guess from batch age and exhausted stock is empty", () => {
  assert.equal(oldestIntakePaint([{ lot_id: 1, age_days: 0, stock_date: "2026-10-06", quantity: 5 }]).bucket, "unknown");
  assert.equal(oldestIntakePaint([item(1, 800, "p1", 0)]).bucket, "empty");
  assert.equal(oldestIntakePaint([]).color, "#FFFFFF");
  assert.equal(oldestIntakePaint([{ ...item(1, 365), available_quantity: 0, reserved_quantity: 4, damaged_quantity: 2 }]).bucket, "365_plus");
});

test("mixed locations keep oldest known age with a separate unknown indicator", () => {
  assert.equal(oldestIntakePaint([item(1, 30), item(2, 365)]).bucket, "365_plus");
  const mixed = oldestIntakePaint([item(1, null), item(2, 400)]);
  assert.equal(mixed.bucket, "365_plus");
  assert.equal(mixed.color, "#A83F46");
  assert.equal(mixed.hasUnknown, true);
  assert.equal(mixed.unknown, false);
  assert.equal(oldestIntakePaint([item(1, null, "p1", 0), item(2, 90)]).bucket, "31_90");
});

test("rack strip keeps old red stock visible when another shelf date is unknown", () => {
  const locations = [location(1, [item(1, 400)]), location(2, [item(2, null)])];
  const paint = rackIntakePaint("A1", locations, warehouseIntakePaints(locations));
  assert.equal(paint.bucket, "365_plus");
  assert.equal(paint.color, "#A83F46");
  assert.equal(paint.hasUnknown, true);
  assert.equal(paint.unknown, false);
  assert.equal(rackIntakePaint("A1", [locations[1]], warehouseIntakePaints([locations[1]])).bucket, "unknown");
});

test("same authorized physical identity refreshes across locations but BOM children stay separate", () => {
  const locations = [location(1, [item(1, 365, "p1:finished")]), location(2, [item(2, 1, "p1:finished")]), location(3, [item(3, 365, "p2:finished")])];
  const paints = warehouseIntakePaints(locations);
  assert.equal(paints[1].bucket, "0_30");
  assert.equal(paints[2].bucket, "0_30");
  assert.equal(paints[3].bucket, "365_plus");
  const children = warehouseIntakePaints([location(1, [{...item(1, 365, "long-piece"), inventory_code: "80011946"}]), location(2, [{...item(2, 1, "short-piece"), inventory_code: "80011946"}])]);
  assert.equal(children[1].bucket, "365_plus");
  assert.equal(children[2].bucket, "0_30");
});

test("product focus paints only its real lots, dims nonmatches and restores overall view", () => {
  const locations = [location(1, [item(1, 1), item(2, 365)]), location(2, [item(3, 181)]), location(3, [])];
  const focused = warehouseIntakePaints(locations, [item(1, 1)]);
  assert.equal(focused[1].bucket, "0_30");
  assert.equal(focused[1].dimmed, false);
  assert.equal(focused[2].dimmed, true);
  assert.equal(warehouseIntakePaints(locations)[1].bucket, "365_plus");
  assert.equal(rackIntakePaint("A1", locations, focused).bucket, "0_30");
  assert.equal(rackIntakePaint("A1", locations, warehouseIntakePaints(locations)).bucket, "365_plus");
});

test("selection has yellow 4px priority over search and hover without changing intake paint", () => {
  assert.deepEqual(intakeOutlineState({selected: true, search: true, hover: true}), {color: "#FACC15", width: 4});
  assert.deepEqual(intakeOutlineState({search: true, hover: true}), {color: "#2563EB", width: 3});
  assert.deepEqual(intakeOutlineState({hover: true}), {color: "#475569", width: 3});
  assert.equal(intakeAgePaint(365).color, "#A83F46");
});

test("actual WebGL map, instance batches and rack elevation consume intake projection", () => {
  const app = readFileSync(new URL("../src/WarehouseTwinApp.tsx", import.meta.url), "utf8");
  const canvas = readFileSync(new URL("../src/EditorCanvas.tsx", import.meta.url), "utf8");
  assert.match(app, /layout=\{intakeVisualLayout \|\| visualLayout\}/);
  assert.match(app, /intakePaints=\{locationIntakePaints\}/);
  assert.match(canvas, /addWarehousePalletBatch/);
  assert.match(canvas, /createIntakeHatchTexture/);
  assert.match(canvas, /outlineBar/);
  assert.match(canvas, /\[\.\.\.INTAKE_AGE_BANDS\]\.reverse\(\)/);
});

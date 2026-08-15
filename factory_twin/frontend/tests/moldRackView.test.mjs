import assert from "node:assert/strict";
import test from "node:test";

import { buildMoldRackView } from "../src/moldRackView.mjs";

const rack = {
  levels: 3,
  bays: 1,
  level_cell_counts: [0, 3, 2]
};

function mold(id, location, guide) {
  return { id, mold_code: `M-${id}`, mold_name: `模具 ${id}`, rack_location: location, location_guide: guide };
}

test("同一格可以投影多件模具且不伪造格内顺序", () => {
  const view = buildMoldRackView(rack, [
    mold(2, "1F-M-R01-L2-G01", { kind: "storage_grid", level: 2, grid: 1 }),
    mold(1, "1F-M-R01-L2-G01", { kind: "storage_grid", level: 2, grid: 1 }),
    mold(3, "1F-M-R01-L2-G02", { kind: "storage_grid", level: 2, grid: 2 })
  ], [1]);

  assert.deepEqual(view.levels[1].cells.map((cell) => cell.items.map((item) => item.mold_code)), [
    ["M-1", "M-2"], ["M-3"], []
  ]);
  assert.equal(view.unmatched_items.length, 0);
});

test("层级位置、货架级位置和超出当前结构的位置不会静默丢失", () => {
  const view = buildMoldRackView(rack, [
    mold(1, "1F-M-R01-L3", { kind: "storage_level", level: 3 }),
    mold(2, "1F-M-R01", { kind: "storage_rack" }),
    mold(3, "1F-M-R01-L3-G03", { kind: "storage_grid", level: 3, grid: 3 }),
    mold(4, "1F-M-R01-L1-G01", { kind: "storage_grid", level: 1, grid: 1 })
  ], [1]);

  assert.deepEqual(view.levels[2].level_only_items.map((item) => item.mold_code), ["M-1"]);
  assert.deepEqual(view.rack_only_items.map((item) => item.mold_code), ["M-2"]);
  assert.deepEqual(view.unmatched_items.map((item) => item.mold_code), ["M-3", "M-4"]);
});

test("旧地图没有逐层格数时继续使用统一 bays", () => {
  const view = buildMoldRackView({ levels: 2, bays: 2 }, [], []);
  assert.deepEqual(view.levels.map((level) => level.cell_count), [2, 2]);
});

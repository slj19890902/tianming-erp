import assert from "node:assert/strict";
import test from "node:test";

import {
  buildMoldRackView,
  buildMoldShelfSpines,
  moldRacksForArea
} from "../src/moldRackView.mjs";

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

test("未绑定正式 ERP 区域的模具区仍按实测区域身份列出 R01 R02", () => {
  const feature = {
    id: "zone-mold-002",
    feature_code: "ZONE-1F-MOLD-002",
    erp_area_code: null
  };
  const racks = [
    { id: "rack-r01", area_feature_id: "zone-mold-002", area_code: "ZONE-1F-MOLD-002", mold_rack_code: "R01" },
    { id: "rack-r02", area_feature_id: "zone-mold-002", area_code: "ZONE-1F-MOLD-002", mold_rack_code: "R02" },
    { id: "rack-r03", area_feature_id: "zone-mold-001", area_code: "ZONE-1F-MOLD-001", mold_rack_code: "R03" },
    { id: "rack-product", area_feature_id: "zone-mold-002", area_code: "ZONE-1F-MOLD-002" }
  ];

  assert.deepEqual(
    moldRacksForArea(feature, racks).map((rack) => rack.mold_rack_code),
    ["R01", "R02"]
  );
});

test("货架书脊按绑定产品展开并保留未绑定模具", () => {
  const spines = buildMoldShelfSpines([
    {
      ...mold(1, "1F-M-R01-L2-G01", { kind: "storage_grid", level: 2, grid: 1 }),
      products: [
        { id: 11, product_code: "21301001", product_name: "白底黑字外箱", customer_name: "天华" },
        { id: 12, product_code: "21301002", product_name: "内衬", customer_name: "天华" }
      ]
    },
    {
      ...mold(2, "1F-M-R01-L2-G01", { kind: "storage_grid", level: 2, grid: 1 }),
      products: []
    }
  ]);

  assert.deepEqual(spines.map((spine) => [spine.mold_id, spine.product_id, spine.code, spine.name]), [
    [1, 11, "21301001", "白底黑字外箱"],
    [1, 12, "21301002", "内衬"],
    [2, null, "M-2", "模具 2（未绑定产品）"]
  ]);
});

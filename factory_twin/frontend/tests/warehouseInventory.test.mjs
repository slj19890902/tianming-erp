import assert from "node:assert/strict";
import test from "node:test";
import {
  buildMappedLocationPallets,
  expandAreaInventory,
  findPalletColumnConflicts,
  filterAreaInventory,
  inventoryAgeLabel,
  inventoryAgeTone,
  inventoryUnitLabel,
  locationLayoutGeometry,
  searchHighlightAreaCodes,
  warehouseSearchFloorSummaries,
  warehouseSearchLocationSummaries,
  warehouseSearchProductKey
} from "../src/warehouseInventory.mjs";

const locations = [
  {
    location_code: "3F-F1-01",
    location_name: "三楼 F1 货架旁",
    floor_code: "3F",
    area_code: "F1",
    pallet: {
      pallet_code: "PLT-3F-001",
      items: [{ lot_id: 1, inventory_code: "CP-001", product_name: "五层加强纸箱", customer_name: "昆山华诚电子有限公司", available_quantity: 800, unit: "只", age_days: 126 }]
    },
    loose_items: []
  },
  {
    location_code: "3F-F1-02",
    location_name: "三楼 F1 地堆",
    floor_code: "3F",
    area_code: "F1",
    pallet: null,
    loose_items: [{ lot_id: 2, inventory_code: "CP-002", product_name: "三层瓦楞外箱", customer_name: "苏州思迈尔包装有限公司", available_quantity: 120, unit: "只", age_days: 8 }]
  },
  {
    location_code: "3F-E1-01",
    location_name: "三楼 E1",
    floor_code: "3F",
    area_code: "E1",
    pallet: null,
    loose_items: [{ lot_id: 3, inventory_code: "OTHER", age_days: 300 }]
  }
];

test("area inventory expands real pallet and loose items without crossing area boundaries", () => {
  const items = expandAreaInventory(locations, "3F", "F1");
  assert.deepEqual(items.map((item) => item.lot_id), [1, 2]);
  assert.equal(items[0].pallet_code, "PLT-3F-001");
  assert.equal(items[1].location_name, "三楼 F1 地堆");
});

test("area inventory filter matches code product customer lot and physical location", () => {
  const items = expandAreaInventory(locations, "3F", "F1");
  assert.deepEqual(filterAreaInventory(items, "cp-002").map((item) => item.lot_id), [2]);
  assert.deepEqual(filterAreaInventory(items, "华诚电子").map((item) => item.lot_id), [1]);
  assert.deepEqual(filterAreaInventory(items, "货架旁").map((item) => item.lot_id), [1]);
  assert.equal(filterAreaInventory(items, "不存在").length, 0);
});

test("inventory age labels distinguish confirmed long age and unknown age", () => {
  assert.equal(inventoryAgeLabel(null), "库龄待确认");
  assert.equal(inventoryAgeLabel(0), "今日入库");
  assert.equal(inventoryAgeLabel(126), "库龄 126 天");
  assert.equal(inventoryAgeTone(null), "unknown");
  assert.equal(inventoryAgeTone(126), "warning");
  assert.equal(inventoryAgeTone(181), "critical");
});

test("inventory units use employee-friendly factory labels without changing quantities", () => {
  assert.equal(inventoryUnitLabel("boxes"), "只");
  assert.equal(inventoryUnitLabel("sheets"), "张");
  assert.equal(inventoryUnitLabel("pieces"), "件");
  assert.equal(inventoryUnitLabel("kg"), "kg");
});

test("full warehouse matches keep all mapped areas highlighted across the active floor", () => {
  const results = [
    { floor_code: "3F", area_code: "A1", position_status: "area_only" },
    { floor_code: "3F", area_code: "A2", position_status: "mapped" },
    { floor_code: "1F", area_code: "P", position_status: "area_only" },
    { floor_code: "3F", area_code: null, position_status: "unplaced" }
  ];
  assert.deepEqual(searchHighlightAreaCodes(results, "3F"), ["A1", "A2"]);
  assert.deepEqual(searchHighlightAreaCodes(results, "1F"), ["P"]);
});

test("warehouse lookup keeps units separate and summarizes every real floor location", () => {
  const finished = { product_id: 99, customer_id: 1, customer_name: "天华", inventory_code: "TM-001", product_name: "纸箱", inventory_type: "finished", unit: "boxes" };
  assert.equal(
    warehouseSearchProductKey(finished),
    warehouseSearchProductKey({ ...finished, customer_name: "天华旧名称", inventory_code: "TM-001-OLD", product_name: "旧快照名称" })
  );
  assert.notEqual(
    warehouseSearchProductKey(finished),
    warehouseSearchProductKey({ ...finished, inventory_type: "semi_finished", unit: "sheets" })
  );
  assert.deepEqual(warehouseSearchFloorSummaries([
    { floor_code: "1F", location_id: 10, quantity: 5 },
    { floor_code: "3F", location_id: 20, quantity: 12 },
    { floor_code: "3F", location_id: 21, quantity: 8 },
    { floor_code: "3F", location_id: 21, quantity: 2 },
    { floor_code: "UNLOCATED", location_name: "待布局", quantity: 3 }
  ]), [
    { floor_code: "1F", quantity: 5, location_count: 1 },
    { floor_code: "3F", quantity: 22, location_count: 2 },
    { floor_code: "UNLOCATED", quantity: 3, location_count: 1 }
  ]);
  assert.deepEqual(warehouseSearchLocationSummaries([
    { floor_code: "3F", area_code: "A1", location_id: 21, location_name: "A1第一位", position_status: "mapped", quantity: 8 },
    { floor_code: "3F", area_code: "A1", location_id: 21, location_name: "A1第一位", position_status: "mapped", quantity: 2 },
    { floor_code: "UNLOCATED", area_code: null, location_id: null, location_name: "待布局", position_status: "unplaced", quantity: 3 }
  ]), [
    { key: "location:21", floor_code: "3F", area_code: "A1", location_id: 21, location_name: "A1第一位", position_status: "mapped", quantity: 10 },
    { key: "UNLOCATED:TEXT:待布局", floor_code: "UNLOCATED", area_code: null, location_id: null, location_name: "待布局", position_status: "unplaced", quantity: 3 }
  ]);
});

test("every visible mapped location receives one stable read-only pallet simulation", () => {
  const features = [{
    id: "zone-a1",
    feature_kind: "zone",
    feature_code: "ZONE-3F-A1",
    erp_area_code: "A1",
    points: [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]
  }];
  const mappedLocations = [
    { location_id: 2, location_code: "A1-L02", location_name: "A1第二位", floor_code: "3F", area_code: "A1", occupancy_status: "empty", position_status: "area_only", pallet: null, loose_items: [] },
    { location_id: 1, location_code: "A1-L01", location_name: "A1第一位", floor_code: "3F", area_code: "A1", occupancy_status: "occupied", position_status: "mapped", pallet: { pallet_code: "PLT-001", items: [] }, loose_items: [] },
    { location_id: 3, location_code: "A1-PENDING", location_name: "待布局", floor_code: "3F", area_code: "A1", occupancy_status: "occupied", position_status: "unplaced", pallet: null, loose_items: [] },
    { location_id: 4, location_code: "X1", location_name: "无区域", floor_code: "3F", area_code: "X1", occupancy_status: "empty", position_status: "area_only", pallet: null, loose_items: [] }
  ];
  const first = buildMappedLocationPallets(features, mappedLocations, "3F", "layout-3f");
  const second = buildMappedLocationPallets(features, mappedLocations, "3F", "layout-3f");
  assert.deepEqual(first, second);
  assert.deepEqual(first.map((item) => item.id), ["erp-location-1", "erp-location-2"]);
  assert.deepEqual(first.map((item) => item.pallet_code), ["A1-L01", "A1-L02"]);
  assert.deepEqual(first.map((item) => item.visual_status), ["waiting", "empty"]);
  assert.ok(first.every((item) => item.x_mm > 0 && item.x_mm < 6000));
  assert.ok(first.every((item) => item.y_mm > 0 && item.y_mm < 4000));
  assert.ok(first.every((item) => item.is_simulated));
});

test("mapped locations use area-relative layout coordinates and convert 2D drags back to percentages", () => {
  const zone = {id: "zone-a1", feature_kind: "zone", feature_code: "ZONE-A1", erp_area_code: "A1", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]};
  const location = {
    location_id: 21, location_code: "A1-L01", location_name: "A1第一位", floor_code: "3F", area_code: "A1",
    position_status: "mapped", occupancy_status: "empty", pallet: null, loose_items: [],
    map_position: {left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 7, z_index: 0}
  };
  const [pallet] = buildMappedLocationPallets([zone], [location], "3F", "layout-3f");
  assert.equal(pallet.x_mm, 2500);
  assert.equal(pallet.y_mm, 4500);
  assert.deepEqual(locationLayoutGeometry(zone, location, 6500, 2250), {
    location_id: 21,
    expected_version: 7,
    left_pct: 40,
    top_pct: 45,
    width_pct: 50,
    height_pct: 20,
    z_index: 0
  });
});

test("mapped pallet rotation follows each measured slot orientation", () => {
  const zone = {id: "zone-fin", feature_kind: "zone", feature_code: "ZONE-1F-FIN", erp_area_code: "FIN", points: [[0, 0], [2400, 0], [2400, 5000], [0, 5000]]};
  const base = {location_code: "FIN-L001", location_name: "成品位", floor_code: "1F", area_code: "FIN", position_status: "mapped", occupancy_status: "empty", pallet: null, loose_items: []};
  const normal = {...base, location_id: 31, map_position: {left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 1, z_index: 0}};
  const rotated = {...base, location_id: 32, location_code: "FIN-L002", map_position: {left_pct: 0, top_pct: 20, width_pct: 41.6667, height_pct: 24, version: 1, z_index: 0}};

  const pallets = buildMappedLocationPallets([zone], [normal, rotated], "1F", "layout-1f");
  assert.equal(pallets[0].rotation_deg, 0);
  assert.equal(pallets[1].rotation_deg, 90);
});

test("mapped pallet locations detect column overlap without moving either object", () => {
  const pallets = [
    { id: "erp-location-1", x_mm: 0, y_mm: 0, width_mm: 1200, depth_mm: 1000, rotation_deg: 0 },
    { id: "erp-location-2", x_mm: 2800, y_mm: 0, width_mm: 1200, depth_mm: 1000, rotation_deg: 90 }
  ];
  const columns = [{
    id: "column-1",
    feature_kind: "structure",
    subtype: "custom_column",
    points: [[-325, 0], [325, 0]],
    width_mm: 700
  }];
  assert.deepEqual(findPalletColumnConflicts(pallets, [], columns), [
    { pallet_id: "erp-location-1", column_id: "column-1" }
  ]);
  assert.equal(pallets[0].x_mm, 0);
  assert.equal(columns[0].points[0][0], -325);
});

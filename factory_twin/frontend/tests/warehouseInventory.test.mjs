import assert from "node:assert/strict";
import test from "node:test";
import {
  buildMeasuredDispatchPallets,
  buildMappedLocationPallets,
  expandAreaInventory,
  findPalletColumnConflicts,
  filterAreaInventory,
  inventoryAgeLabel,
  inventoryAgeTone,
  inventoryLocationItems,
  inventoryLocationPallets,
  inventoryUnitLabel,
  locationLayoutGeometry,
  searchHighlightAreaCodes,
  warehouseSearchFloorSummaries,
  warehouseSearchLocationSummaries,
  warehouseSearchProductKey,
  singleLocationPallet
} from "../src/warehouseInventory.mjs";
import {
  buildMoveBatchPayload,
  intersectMappedMoveTargets,
  mergeLocationInventoryItems,
  resolveMoveDropTarget,
  upsertMoveDraft
} from "../src/warehouseMoveDraft.mjs";
import {
  buildPalletMergeBatchPayload,
  normalizePalletMergeCandidate,
  palletMergeCompatibility,
  palletMergeTargetChoices,
  togglePalletMergeSource
} from "../src/warehousePalletMergeDraft.mjs";

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
  assert.equal(pallet.width_mm, 400);
  assert.equal(pallet.depth_mm, 400);
  assert.equal(pallet.is_logical_anchor, true);
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
  assert.deepEqual(pallets.map((item) => [Math.round(item.width_mm), Math.round(item.depth_mm)]), [[1200, 1000], [1200, 1000]]);
});

test("confirmed-capacity logical positions render as small measured anchors instead of full pallet outlines", () => {
  const zone = {id: "zone-a1", feature_kind: "zone", feature_code: "ZONE-A1", erp_area_code: "A1", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]};
  const location = {
    location_id: 41, location_code: "A1-L041", location_name: "A1逻辑位", floor_code: "3F", area_code: "A1",
    position_status: "mapped", occupancy_status: "empty", pallet: null, loose_items: [],
    map_position: {left_pct: 10, top_pct: 10, width_pct: 2, height_pct: 4, version: 1, z_index: 0}
  };

  const [anchor] = buildMappedLocationPallets([zone], [location], "3F", "layout-3f");
  assert.equal(anchor.width_mm, 200);
  assert.equal(anchor.depth_mm, 200);
  assert.equal(anchor.is_logical_anchor, true);
  assert.match(anchor.status_note, /逻辑点位/);
});

test("persisted layout kind overrides the bounded legacy footprint inference", () => {
  const zone = {id: "zone-a1", feature_kind: "zone", feature_code: "ZONE-A1", erp_area_code: "A1", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]};
  const base = {
    location_code: "A1-L001", location_name: "A1点位", floor_code: "3F", area_code: "A1",
    position_status: "mapped", occupancy_status: "empty", pallet: null, loose_items: []
  };
  const locations = [
    {...base, location_id: 51, location_code: "A1-L051", map_position: {left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 1, z_index: 0, layout_kind: "physical_pallet"}},
    {...base, location_id: 52, location_code: "A1-L052", map_position: {left_pct: 50, top_pct: 0, width_pct: 12, height_pct: 20, version: 1, z_index: 0, layout_kind: "logical_anchor"}}
  ];

  const pallets = buildMappedLocationPallets([zone], locations, "3F", "layout-3f");
  assert.equal(pallets[0].is_logical_anchor, false);
  assert.equal(pallets[1].is_logical_anchor, true);
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

test("move targets are the intersection of empty API candidates and mapped dashboard locations", () => {
  const candidates = [
    { id: 1, is_empty: true },
    { id: 2, is_empty: true },
    { id: 3, is_empty: true },
    { id: 4, is_empty: false }
  ];
  const dashboard = [
    { location_id: 1, floor_code: "1F", area_code: "FIN", location_code: "FIN-01", is_active: true, occupancy_status: "empty", position_status: "mapped", map_position: { left_pct: 0, top_pct: 0, width_pct: 20, height_pct: 20 } },
    { location_id: 2, floor_code: "3F", area_code: "A1", location_code: "A1-01", is_active: true, occupancy_status: "empty", position_status: "unplaced", map_position: null },
    { location_id: 3, floor_code: "3F", area_code: "A1", location_code: "A1-02", is_active: true, occupancy_status: "occupied", position_status: "mapped", map_position: { left_pct: 20, top_pct: 0, width_pct: 20, height_pct: 20 } },
    { location_id: 4, floor_code: "3F", area_code: "A1", location_code: "A1-03", is_active: true, occupancy_status: "empty", position_status: "mapped", map_position: { left_pct: 40, top_pct: 0, width_pct: 20, height_pct: 20 } }
  ];
  assert.deepEqual(intersectMappedMoveTargets(candidates, dashboard).map((item) => item.location_id), [1]);
  assert.deepEqual(intersectMappedMoveTargets(candidates, dashboard, [1]), []);
  assert.deepEqual(intersectMappedMoveTargets(candidates, dashboard, [], [1]), []);
});

test("location move source list keeps pallet order, appends loose lots, and de-duplicates lot ids", () => {
  const palletItems = [{ lot_id: 10, label: "pallet-a" }, { lot_id: 11, label: "pallet-b" }];
  const looseItems = [{ lot_id: 11, label: "duplicate" }, { lot_id: 12, label: "loose-c" }, { label: "legacy-without-id" }];
  assert.deepEqual(mergeLocationInventoryItems(palletItems, looseItems), [
    { lot_id: 10, label: "pallet-a" },
    { lot_id: 11, label: "pallet-b" },
    { lot_id: 12, label: "loose-c" },
    { label: "legacy-without-id" }
  ]);
  assert.deepEqual(palletItems, [{ lot_id: 10, label: "pallet-a" }, { lot_id: 11, label: "pallet-b" }]);
  assert.deepEqual(looseItems, [{ lot_id: 11, label: "duplicate" }, { lot_id: 12, label: "loose-c" }, { label: "legacy-without-id" }]);
});

test("shared dispatch location keeps every system pallet without fabricating map positions", () => {
  const zone = { id: "zone-dispatch", feature_kind: "zone", feature_code: "ZONE-DISPATCH", erp_area_code: "DISPATCH", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]] };
  const shared = {
    location_id: 49,
    location_code: "F1-DISPATCH-01",
    location_name: "一楼成品待送区",
    floor_code: "1F",
    area_code: "DISPATCH",
    position_status: "mapped",
    occupancy_status: "occupied",
    map_position: { left_pct: 10, top_pct: 20, width_pct: 20, height_pct: 30, version: 4, z_index: 0 },
    pallet: null,
    pallets: [
      { pallet_id: 22, pallet_code: "PLT-F1-PC-22", version: 1, items: [{ lot_id: 202, product_name: "五层加强纸箱" }] },
      { pallet_id: 11, pallet_code: "PLT-F1-PC-11", version: 2, items: [{ lot_id: 101, product_name: "三层瓦楞外箱" }] }
    ],
    loose_items: [{ lot_id: 202, product_name: "重复投影" }, { lot_id: 303, product_name: "历史散存" }]
  };

  assert.deepEqual(inventoryLocationPallets(shared).map((item) => item.pallet_id), [11, 22]);
  assert.equal(singleLocationPallet(shared), null);
  assert.deepEqual(inventoryLocationItems(shared).map((item) => item.lot_id), [101, 202, 303]);

  const mapped = buildMappedLocationPallets([zone], [shared], "1F", "layout-1f");
  assert.equal(mapped.length, 1);
  assert.equal(mapped[0].id, "erp-location-49");
  assert.match(mapped[0].name, /2 块系统栈板/);
  assert.match(mapped[0].status_note, /右侧逐块选择/);

  const expanded = expandAreaInventory([shared], "1F", "DISPATCH");
  assert.deepEqual(expanded.map((item) => [item.lot_id, item.pallet_code]), [
    [101, "PLT-F1-PC-11"],
    [202, "PLT-F1-PC-22"],
    [303, null]
  ]);
});

test("measured dispatch zones project every real system pallet with live product quantity", () => {
  const zones = [
    { id: "fin-2", feature_kind: "zone", feature_code: "FIN-002", subtype: "finished_wait_delivery", points: [[12000, 0], [22000, 0], [22000, 5000], [12000, 5000]] },
    { id: "fin-1", feature_kind: "zone", feature_code: "FIN-001", subtype: "finished_wait_delivery", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]] },
    { id: "raw-1", feature_kind: "zone", feature_code: "RAW-001", subtype: "raw_material", points: [[0, 6000], [10000, 6000], [10000, 9000], [0, 9000]] }
  ];
  const dispatch = {
    location_id: 401,
    location_code: "F1-DISPATCH-01",
    location_name: "一楼厂外成品待送区",
    floor_code: "1F",
    area_code: "DISPATCH",
    pallets: [
      { pallet_id: 12, pallet_code: "PLT-PC-12", version: 3, items: [{ product_name: "五层纸箱", customer_name: "客户乙", reserved_quantity: 80, unit: "boxes" }] },
      { pallet_id: 10, pallet_code: "PLT-PC-10", version: 2, items: [{ product_name: "三层纸箱", customer_name: "客户甲", available_quantity: 20, reserved_quantity: 30, unit: "boxes" }] },
      { pallet_id: 11, pallet_code: "PLT-PC-11", version: 1, items: [{ product_name: "模切纸箱", customer_name: "客户丙", quantity: 60, unit: "boxes" }] }
    ],
    loose_items: []
  };

  const first = buildMeasuredDispatchPallets(zones, dispatch, "1F", "layout-1f");
  const second = buildMeasuredDispatchPallets(zones, dispatch, "1F", "layout-1f");
  assert.deepEqual(first, second);
  assert.deepEqual(first.map((item) => item.id), ["erp-dispatch-pallet-10", "erp-dispatch-pallet-12", "erp-dispatch-pallet-11"]);
  assert.deepEqual(first.map((item) => item.zone_code), ["FIN-001", "FIN-001", "FIN-002"]);
  assert.match(first[0].name, /三层纸箱 · 50 只/);
  assert.match(first[0].status_note, /客户甲/);
  assert.ok(first.every((item) => item.is_simulated === false));
  assert.ok(first.slice(0, 2).every((item) => item.x_mm > 0 && item.x_mm < 10000 && item.y_mm > 0 && item.y_mm < 5000));
  assert.ok(first.slice(2).every((item) => item.x_mm > 12000 && item.x_mm < 22000 && item.y_mm > 0 && item.y_mm < 5000));
  assert.deepEqual(buildMeasuredDispatchPallets(zones, dispatch, "3F", "layout-3f"), []);
});

test("ordinary single-pallet location keeps the legacy one-card move path", () => {
  const pallet = { pallet_id: 7, pallet_code: "PLT-3F-007", version: 3, items: [{ lot_id: 70 }] };
  const location = { pallet, pallets: [pallet], loose_items: [] };
  assert.equal(singleLocationPallet(location)?.pallet_id, 7);
  assert.deepEqual(inventoryLocationPallets(location), [pallet]);
});

test("same-floor drag resolves one published empty location without changing geometry", () => {
  const features = [{ feature_kind: "zone", erp_area_code: "A1", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]] }];
  const target = { location_id: 8, floor_code: "3F", area_code: "A1", occupancy_status: "empty", position_status: "mapped", map_position: { version: 4, left_pct: 10, top_pct: 20, width_pct: 20, height_pct: 30 } };
  const before = structuredClone(target);
  assert.equal(resolveMoveDropTarget(features, [target], "3F", 2000, 3250).target?.location_id, 8);
  assert.match(resolveMoveDropTarget(features, [target], "3F", 9000, 1000).error, /空货位|三级选择/);
  assert.deepEqual(target, before);
});

test("move drafts replace one source, reject target collision, and build one confirmed batch", () => {
  const base = {
    client_item_id: "client-1", source_key: "pallet:10", operation: "pallet_move", pallet_id: 10,
    expected_version: 2, source_location_id: 1, source_floor_code: "1F", source_area_code: "FIN", source_location_code: "FIN-01", source_location_name: "成品位 1",
    target_location_id: 2, expected_target_layout_version: 2, target_floor_code: "3F", target_area_code: "A1", target_location_code: "A1-01", target_location_name: "三楼 A1-01",
    inventory_code: "PAL-10", product_name: "整栈板", customer_name: "天华", unit: "boxes"
  };
  const first = upsertMoveDraft([], base);
  const replacement = upsertMoveDraft(first.items, { ...base, client_item_id: "must-not-replace-stable-id", target_location_id: 3, expected_target_layout_version: 3, target_location_code: "A1-02" });
  assert.equal(replacement.items.length, 1);
  assert.equal(replacement.items[0].client_item_id, "client-1");
  assert.equal(replacement.items[0].target_location_id, 3);
  const collision = upsertMoveDraft(replacement.items, { ...base, client_item_id: "client-2", source_key: "lot:20", operation: "lot_transfer", pallet_id: undefined, lot_id: 20, quantity: 6, target_location_id: 3 });
  assert.match(collision.error, /占用/);
  assert.deepEqual(buildMoveBatchPayload("batch-key-123", [
    replacement.items[0],
    { ...base, client_item_id: "client-2", source_key: "lot:20", operation: "lot_transfer", pallet_id: undefined, lot_id: 20, quantity: 6, target_location_id: 4, expected_target_layout_version: 4 }
  ]), {
    idempotency_key: "batch-key-123",
    confirmed: true,
    items: [
      { client_item_id: "client-1", operation: "pallet_move", pallet_id: 10, expected_version: 2, target_location_id: 3, expected_target_layout_version: 3 },
      { client_item_id: "client-2", operation: "lot_transfer", lot_id: 20, quantity: 6, expected_version: 2, target_location_id: 4, expected_target_layout_version: 4 }
    ]
  });
});

function mergeLocation(overrides = {}) {
  return {
    location_id: 49,
    location_code: "F1-DISPATCH-01",
    location_name: "一楼成品待送区",
    floor_code: "1F",
    area_code: "DISPATCH",
    warehouse_type: "finished",
    storage_type: "pallet_ground",
    is_active: true,
    position_status: "mapped",
    map_position: { left_pct: 10, top_pct: 20, width_pct: 20, height_pct: 30 },
    ...overrides
  };
}

function mergePallet(palletId, overrides = {}) {
  return {
    pallet_id: palletId,
    pallet_code: `PLT-${palletId}`,
    version: palletId,
    items: [{
      lot_id: 100 + palletId,
      version: 2,
      product_id: palletId,
      inventory_code: `CP-${palletId}`,
      customer_id: 7,
      customer_name: "苏州思迈尔包装有限公司",
      inventory_type: "finished",
      unit: "boxes",
      status: "active",
      available_quantity: 40,
      reserved_quantity: 10,
      damaged_quantity: 0
    }],
    ...overrides
  };
}

test("pallet merge candidates keep real locations, native units, and different inventory codes", () => {
  const first = normalizePalletMergeCandidate(mergeLocation(), mergePallet(11));
  const second = normalizePalletMergeCandidate(mergeLocation({ location_id: 50, location_code: "F1-DISPATCH-02" }), mergePallet(22));
  assert.equal(first.error, null);
  assert.equal(first.candidate.location_id, 49);
  assert.equal(first.candidate.total_quantity, 50);
  assert.equal(second.candidate.location_id, 50);
  assert.equal(palletMergeCompatibility(first.candidate, second.candidate).compatible, true);
  const selected = togglePalletMergeSource(togglePalletMergeSource([], first.candidate).items, second.candidate);
  assert.deepEqual(selected.items.map((item) => item.pallet_id), [11, 22]);
  assert.deepEqual(togglePalletMergeSource(selected.items, first.candidate).items.map((item) => item.pallet_id), [22]);
});

test("pallet merge candidates fail closed for snapshots, mixed units, frozen state, and quality", () => {
  const source = normalizePalletMergeCandidate(mergeLocation(), mergePallet(11)).candidate;
  const snapshot = mergePallet(12, { items: [{ customer_id: 7, inventory_type: "finished", unit: "boxes", status: "active", quantity: 8 }] });
  assert.match(normalizePalletMergeCandidate(mergeLocation(), snapshot).error, /正式批次/);

  const mixedUnit = mergePallet(13, { items: [
    mergePallet(13).items[0],
    { ...mergePallet(14).items[0], lot_id: 114, unit: "sheets" }
  ] });
  assert.match(normalizePalletMergeCandidate(mergeLocation(), mixedUnit).error, /原生单位/);

  const missingOwner = mergePallet(16, { items: [
    mergePallet(16).items[0],
    { ...mergePallet(17).items[0], lot_id: 117, customer_id: null }
  ] });
  assert.match(normalizePalletMergeCandidate(mergeLocation(), missingOwner).error, /客户归属/);

  const frozen = normalizePalletMergeCandidate(mergeLocation(), mergePallet(14, { items: [{ ...mergePallet(14).items[0], status: "frozen" }] })).candidate;
  assert.match(palletMergeCompatibility(source, frozen).error, /冻结/);
  const damaged = normalizePalletMergeCandidate(mergeLocation(), mergePallet(15, { items: [{ ...mergePallet(15).items[0], damaged_quantity: 2 }] }));
  assert.match(damaged.error, /质量/);
  assert.deepEqual(togglePalletMergeSource([source], frozen).items, [source]);
});

test("pallet merge target is chosen from the selected set and omitted from batch sources", () => {
  const sourceOne = normalizePalletMergeCandidate(mergeLocation(), mergePallet(11)).candidate;
  const sourceTwo = normalizePalletMergeCandidate(mergeLocation(), mergePallet(22)).candidate;
  const target = normalizePalletMergeCandidate(
    mergeLocation({ location_id: 70, location_code: "3F-A1-01", location_name: "三楼 A1-01", floor_code: "3F", area_code: "A1" }),
    mergePallet(33)
  ).candidate;
  assert.deepEqual(palletMergeTargetChoices([sourceOne, sourceTwo, target]).map((item) => item.pallet_id), [11, 22, 33]);
  assert.deepEqual(buildPalletMergeBatchPayload("merge-batch-key", [
    { ...sourceOne, client_item_id: "merge-source-11" },
    { ...sourceTwo, client_item_id: "merge-source-22" },
    { ...target, client_item_id: "merge-target-33" }
  ], target), {
    idempotency_key: "merge-batch-key",
    confirmed: true,
    target_pallet_id: 33,
    expected_target_version: 33,
    sources: [
      { client_item_id: "merge-source-11", pallet_id: 11, expected_version: 11 },
      { client_item_id: "merge-source-22", pallet_id: 22, expected_version: 22 }
    ]
  });
  assert.deepEqual(buildPalletMergeBatchPayload("two-pallet-key", [
    { ...sourceOne, client_item_id: "merge-source-11" },
    { ...target, client_item_id: "merge-target-33" }
  ], target).sources, [
    { client_item_id: "merge-source-11", pallet_id: 11, expected_version: 11 }
  ]);
});

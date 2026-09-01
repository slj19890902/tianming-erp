import assert from "node:assert/strict";
import test from "node:test";
import {
  buildMeasuredDispatchPallets,
  buildMappedLocationPallets,
  employeeAreaName,
  employeeLocationName,
  expandAreaInventory,
  findPalletColumnConflicts,
  filterAreaInventory,
  inventoryAgeLabel,
  inventoryAgeTone,
  inventoryLocationItems,
  inventoryLocationPallets,
  inventoryHasPhysicalQuantity,
  inventoryPhysicalQuantity,
  inventoryUnitLabel,
  locationLayoutGeometry,
  mergePublishedFeatureGeometry,
  normalizeInventoryLocationProjection,
  normalizeStandardPalletContract,
  searchHighlightAreaCodes,
  standardPalletDisplayIssue,
  standardPalletContractsMatch,
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
  palletMergeSuggestionProductKey,
  palletMergeSuggestionMatchesFilter,
  palletMergeTargetChoices,
  togglePalletMergeSource
} from "../src/warehousePalletMergeDraft.mjs";
import {
  buildStocktakeBatchPayload,
  clearStocktakeDrafts,
  removeStocktakeDraft,
  stocktakeAddBlockReason,
  stocktakeBlockResolution,
  stocktakeDecreaseBlockReason,
  stocktakeExistingProductLocations,
  stocktakeLocationBlockReason,
  upsertStocktakeDraft,
  validateStocktakeDraft
} from "../src/warehouseStocktakeDraft.mjs";

const LEGACY_RIGHT_AREA_CODES = [
  "A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2", "CD1", "D1", "D2",
  "DE1", "E1", "E2", "E3", "E4", "F1", "F12", "F2", "F3", "F34", "F4"
];

test("employee area labels always identify 3F A-F as right-side and keep descriptions", () => {
  for (const areaCode of LEGACY_RIGHT_AREA_CODES) {
    assert.equal(
      employeeAreaName(
        { area_code: areaCode, formal_area_name: `${areaCode} 区` },
        { floorCode: "3F" }
      ),
      `右区${areaCode}`
    );
  }
  assert.equal(
    employeeAreaName(
      { area_code: "A1", formal_area_name: "三楼北侧成品区" },
      { floorCode: "3F" }
    ),
    "右区A1·北侧成品区"
  );
  assert.equal(
    employeeAreaName(
      { area_code: "RAW-001", formal_area_name: "左区L3 原料区（上段）" },
      { floorCode: "3F" }
    ),
    "左区L3 原料区（上段）"
  );
  assert.equal(
    employeeAreaName(
      { area_code: "A1", formal_area_name: "A1 区" },
      { floorCode: "1F" }
    ),
    "A1 区"
  );
  assert.equal(
    employeeAreaName(
      { employee_area_name: "右区A1", area_code: "A1", area_name: "A1 区" },
      { floorCode: "3F" }
    ),
    "右区A1"
  );
});

const STANDARD_PALLET = {
  contract_version: "standard-pallet-v1",
  width_mm: 1200,
  depth_mm: 1000,
  height_mm: 150
};

test("employee location labels fail closed instead of leaking internal codes", () => {
  assert.equal(
    employeeLocationName({ location_name: "  三楼东侧第二排第三位  ", location_code: "C1-L01" }),
    "三楼东侧第二排第三位"
  );
  assert.equal(
    employeeLocationName({ location_name: "   ", location_code: "C1-L01" }),
    "位置名称待完善"
  );
  assert.equal(employeeLocationName(null), "位置名称待完善");
  assert.equal(
    employeeLocationName({
      employee_location_name: "三楼 A1成品区·左侧第1位",
      current_address_name: "三楼 A1旧名称",
      location_name: "A1-R01"
    }),
    "三楼 A1成品区·左侧第1位"
  );
});

test("standard pallet contract fails closed when missing malformed or inconsistent", () => {
  assert.deepEqual(normalizeStandardPalletContract(STANDARD_PALLET), STANDARD_PALLET);
  assert.equal(normalizeStandardPalletContract(null), null);
  assert.equal(normalizeStandardPalletContract({ ...STANDARD_PALLET, height_mm: 0 }), null);
  assert.equal(standardPalletContractsMatch(STANDARD_PALLET, { ...STANDARD_PALLET }), true);
  assert.equal(standardPalletContractsMatch(STANDARD_PALLET, { ...STANDARD_PALLET, height_mm: 160 }), false);

  const zone = {
    id: "zone-fin",
    feature_kind: "zone",
    feature_code: "ZONE-1F-FIN",
    erp_area_code: "FIN",
    points: [[0, 0], [12000, 0], [12000, 6000], [0, 6000]]
  };
  const location = {
    location_id: 99,
    location_code: "FIN-L099",
    location_name: "成品位",
    floor_code: "1F",
    area_code: "FIN",
    position_status: "mapped",
    occupancy_status: "occupied",
    map_position: { left_pct: 0, top_pct: 0, width_pct: 10, height_pct: 20, layout_kind: "physical_pallet" },
    pallet: null,
    pallets: [],
    loose_items: []
  };
  assert.deepEqual(buildMappedLocationPallets([zone], [location], "1F", null, "layout-1f"), []);
});

test("floor switching never reports a transient pallet-size failure", () => {
  assert.equal(standardPalletDisplayIssue({
    loading: true,
    requestedFloorCode: "3F",
    layoutFloorCode: "1F",
    layoutContract: null,
    dashboardContract: STANDARD_PALLET
  }), "");
  assert.equal(standardPalletDisplayIssue({
    loading: false,
    requestedFloorCode: "3F",
    layoutFloorCode: "3F",
    layoutContract: STANDARD_PALLET,
    dashboardContract: STANDARD_PALLET
  }), "");
  assert.match(standardPalletDisplayIssue({
    loading: false,
    requestedFloorCode: "3F",
    layoutFloorCode: "3F",
    layoutContract: null,
    dashboardContract: STANDARD_PALLET
  }), /标准栈板尺寸合同缺失/);
});

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
  assert.equal(inventoryUnitLabel("sets"), "套");
  assert.equal(inventoryUnitLabel("kg"), "kg");
});

test("physical inventory quantity keeps reserved-only and damaged-only lots visible", () => {
  assert.equal(inventoryPhysicalQuantity({ available_quantity: 0, reserved_quantity: 18, damaged_quantity: 0 }), 18);
  assert.equal(inventoryPhysicalQuantity({ quantity: 0, available_quantity: 0, reserved_quantity: 18, damaged_quantity: 0 }), 18);
  assert.equal(inventoryPhysicalQuantity({ available_quantity: 0, reserved_quantity: 0, damaged_quantity: 7 }), 7);
  assert.equal(inventoryPhysicalQuantity({ quantity: 25, available_quantity: 5, reserved_quantity: 10, damaged_quantity: 10 }), 25);
});

test("location projection keeps every positive inventory state and removes current-empty ghost pallets", () => {
  const pallet = (id, item) => ({
    pallet_id: id,
    pallet_code: `PLT-${id}`,
    version: 1,
    items: [item]
  });
  const location = {
    location_id: 88,
    location_code: "A1-L05",
    floor_code: "3F",
    area_code: "A1",
    map_feature_id: "zone-a1",
    position_status: "mapped",
    occupancy_status: "occupied",
    map_position: { left_pct: 10, top_pct: 20, width_pct: 8, height_pct: 8, version: 2 },
    pallets: [
      pallet(1, { lot_id: 1, available_quantity: 3, reserved_quantity: 0, damaged_quantity: 0 }),
      pallet(2, { lot_id: 2, available_quantity: 0, reserved_quantity: 4, damaged_quantity: 0 }),
      pallet(3, { lot_id: 3, available_quantity: 0, reserved_quantity: 0, damaged_quantity: 5 }),
      pallet(4, { lot_id: 4, quantity: 0, available_quantity: 0, reserved_quantity: 0, damaged_quantity: 0 })
    ],
    loose_items: [
      { lot_id: 5, quantity: 0, available_quantity: 0, reserved_quantity: 0, damaged_quantity: 0 },
      { lot_id: 6, available_quantity: 0, reserved_quantity: 2, damaged_quantity: 0 }
    ]
  };

  assert.equal(inventoryHasPhysicalQuantity(location.pallets[0].items[0]), true);
  assert.equal(inventoryHasPhysicalQuantity(location.pallets[1].items[0]), true);
  assert.equal(inventoryHasPhysicalQuantity(location.pallets[2].items[0]), true);
  assert.equal(inventoryHasPhysicalQuantity(location.pallets[3].items[0]), false);

  const projected = normalizeInventoryLocationProjection(location);
  assert.deepEqual(inventoryLocationPallets(projected).map((item) => item.pallet_id), [1, 2, 3]);
  assert.deepEqual(inventoryLocationItems(projected).map((item) => item.lot_id), [1, 2, 3, 6]);
  assert.equal(projected.occupancy_status, "occupied");

  const emptied = normalizeInventoryLocationProjection({
    ...location,
    pallets: [location.pallets[3]],
    loose_items: [location.loose_items[0]]
  });
  assert.deepEqual(emptied.pallets, []);
  assert.equal(emptied.pallet, null);
  assert.deepEqual(emptied.loose_items, []);
  assert.equal(emptied.occupancy_status, "empty");
});

test("full delivery leaves a mapped empty location while partial reserved and damaged stock stay physical", () => {
  const zone = {
    id: "zone-a1",
    feature_kind: "zone",
    feature_code: "A1",
    erp_area_code: "A1",
    points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]
  };
  const location = (id, left, item) => ({
    location_id: id,
    location_code: `A1-L0${id}`,
    location_name: `三楼 A1 第${id}位`,
    floor_code: "3F",
    area_code: "A1",
    map_feature_id: "zone-a1",
    position_status: "mapped",
    occupancy_status: "occupied",
    map_position: { left_pct: left, top_pct: 20, width_pct: 8, height_pct: 8, version: 1 },
    pallets: [{ pallet_id: id, pallet_code: `PLT-${id}`, version: 1, items: [item] }],
    loose_items: []
  });
  const rows = [
    location(1, 10, { lot_id: 11, available_quantity: 0, reserved_quantity: 0, damaged_quantity: 0 }),
    location(2, 30, { lot_id: 22, available_quantity: 6, reserved_quantity: 0, damaged_quantity: 0 }),
    location(3, 50, { lot_id: 33, available_quantity: 0, reserved_quantity: 7, damaged_quantity: 0 }),
    location(4, 70, { lot_id: 44, available_quantity: 0, reserved_quantity: 0, damaged_quantity: 8 })
  ].map(normalizeInventoryLocationProjection);

  const mapped = buildMappedLocationPallets([zone], rows, "3F", STANDARD_PALLET, "layout-3f");
  assert.equal(mapped.length, 4);
  assert.deepEqual(mapped.map((item) => item.visual_status), ["empty", "waiting", "waiting", "waiting"]);
  assert.deepEqual(mapped.map((item) => item.visual_kind), ["location_anchor", "physical_pallet", "physical_pallet", "physical_pallet"]);
  assert.deepEqual(rows.map((item) => item.occupancy_status), ["empty", "occupied", "occupied", "occupied"]);
  assert.deepEqual(mapped.map((item) => item.candidate_status_color), ["#16a34a", "#2563eb", "#2563eb", "#2563eb"]);
});

test("area planning renders every mapped empty location as a full draggable slot", () => {
  const zone = {
    id: "zone-e2",
    feature_kind: "zone",
    feature_code: "E2",
    erp_area_code: "E2",
    points: [[0, 0], [12000, 0], [12000, 6000], [0, 6000]]
  };
  const locations = [
    {
      location_id: 201,
      location_code: "3F-E2-P01-01",
      location_name: "三楼 右区E2·E2-1",
      floor_code: "3F",
      area_code: "E2",
      storage_type: "ground",
      map_feature_id: "zone-e2",
      position_status: "mapped",
      occupancy_status: "empty",
      map_position: { left_pct: 10, top_pct: 20, width_pct: 10, height_pct: 20, version: 1, layout_kind: "physical_pallet" },
      pallets: [],
      loose_items: []
    },
    {
      location_id: 202,
      location_code: "3F-E2-P01-02",
      location_name: "三楼 右区E2·E2-2",
      floor_code: "3F",
      area_code: "E2",
      storage_type: "ground",
      map_feature_id: "zone-e2",
      position_status: "mapped",
      occupancy_status: "occupied",
      map_position: { left_pct: 30, top_pct: 20, width_pct: 10, height_pct: 20, version: 1 },
      pallets: [{ pallet_id: 202, pallet_code: "PLT-E2-202", version: 1, items: [{ lot_id: 202, available_quantity: 8 }] }],
      loose_items: []
    }
  ].map(normalizeInventoryLocationProjection);

  const planned = buildMappedLocationPallets(
    [zone],
    locations,
    "3F",
    STANDARD_PALLET,
    "layout-3f",
    true
  );

  assert.equal(planned.length, 2);
  assert.deepEqual(planned.map((item) => item.is_logical_anchor), [true, false]);
  assert.deepEqual(planned.map((item) => item.is_planning_location_slot), [true, true]);
  assert.equal(planned[0].planning_slot_width_mm, 1200);
  assert.equal(planned[0].planning_slot_depth_mm, 1000);
  assert.equal(planned[0].width_mm, 0);
  assert.equal(planned[0].depth_mm, 0);
  assert.deepEqual(planned.map((item) => item.candidate_status_color), ["#16a34a", "#2563eb"]);
});

test("unmatched goods and known-location discrepancies keep formal positions red", () => {
  const zone = {
    id: "zone-a1",
    feature_kind: "zone",
    feature_code: "A1",
    erp_area_code: "A1",
    points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]
  };
  const location = normalizeInventoryLocationProjection({
    location_id: 89,
    location_code: "A1-L06",
    location_name: "三楼 右区A1·A1-6",
    floor_code: "3F",
    area_code: "A1",
    source_version: "V11",
    position_status: "mapped",
    occupancy_status: "empty",
    map_position: { left_pct: 10, top_pct: 20, width_pct: 8, height_pct: 8, version: 2 },
    pallets: [],
    loose_items: [],
    has_unmatched_inventory_observation: true,
    unmatched_inventory_observation_count: 2
  });

  const [mapped] = buildMappedLocationPallets([zone], [location], "3F", STANDARD_PALLET, "layout-3f");
  assert.equal(mapped.visual_status, "empty");
  assert.equal(mapped.color, "#b91c1c");
  assert.match(mapped.status_note, /现场库存待核对/);
  assert.match(mapped.status_note, /2 条红色异常/);

  const knownMismatch = normalizeInventoryLocationProjection({
    ...location,
    location_id: 90,
    location_code: "A1-L07",
    has_unmatched_inventory_observation: false,
    unmatched_inventory_observation_count: 0,
    has_location_discrepancy: true,
    location_discrepancy_count: 1
  });
  const [mismatchMarker] = buildMappedLocationPallets([zone], [knownMismatch], "3F", STANDARD_PALLET, "layout-3f");
  assert.equal(mismatchMarker.color, "#b91c1c");
  assert.match(mismatchMarker.status_note, /1 条红色异常/);
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

test("only locations with measured geometry are drawn on the warehouse map", () => {
  const features = [{
    id: "zone-a1",
    feature_kind: "zone",
    feature_code: "ZONE-3F-A1",
    erp_area_code: "A1",
    points: [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]
  }];
  const mappedLocations = [
    { location_id: 2, location_code: "A1-L02", location_name: "A1第二位", floor_code: "3F", area_code: "A1", occupancy_status: "empty", position_status: "area_only", pallet: null, loose_items: [] },
    { location_id: 1, location_code: "A1-L01", location_name: "A1第一位", floor_code: "3F", area_code: "A1", source_version: "TWIN_V1", map_feature_id: "zone-a1", occupancy_status: "occupied", position_status: "mapped", map_position: { left_pct: 10, top_pct: 10, width_pct: 20, height_pct: 20, version: 1, z_index: 0 }, pallet: { pallet_code: "PLT-001", items: [{ lot_id: 1, quantity: 1 }] }, loose_items: [] },
    { location_id: 3, location_code: "A1-PENDING", location_name: "待布局", floor_code: "3F", area_code: "A1", occupancy_status: "occupied", position_status: "unplaced", pallet: null, loose_items: [] },
    { location_id: 4, location_code: "X1", location_name: "无区域", floor_code: "3F", area_code: "X1", occupancy_status: "empty", position_status: "area_only", pallet: null, loose_items: [] }
  ];
  const first = buildMappedLocationPallets(features, mappedLocations, "3F", STANDARD_PALLET, "layout-3f");
  const second = buildMappedLocationPallets(features, mappedLocations, "3F", STANDARD_PALLET, "layout-3f");
  assert.deepEqual(first, second);
  assert.deepEqual(first.map((item) => item.id), ["erp-location-1"]);
  assert.deepEqual(first.map((item) => item.pallet_code), ["A1-L01"]);
  assert.deepEqual(first.map((item) => item.visual_status), ["waiting"]);
  assert.ok(first.every((item) => item.x_mm > 0 && item.x_mm < 6000));
  assert.ok(first.every((item) => item.y_mm > 0 && item.y_mm < 4000));
  assert.ok(first.every((item) => item.is_simulated === false));
});

test("TWIN locations resolve the exact backend map feature and never fall back by area code", () => {
  const features = [
    {
      id: "zone-a1-stale",
      feature_kind: "zone",
      feature_code: "ZONE-A1-STALE",
      erp_area_code: "A1",
      points: [[0, 0], [4000, 0], [4000, 4000], [0, 4000]]
    },
    {
      id: "zone-a1-current",
      feature_kind: "zone",
      feature_code: "ZONE-A1-CURRENT",
      erp_area_code: "A1",
      points: [[10000, 0], [14000, 0], [14000, 4000], [10000, 4000]]
    }
  ];
  const location = {
    location_id: 81,
    location_code: "A1-L081",
    location_name: "A1当前点位",
    floor_code: "3F",
    area_code: "A1",
    source_version: "TWIN_V1",
    map_feature_id: "zone-a1-current",
    position_status: "mapped",
    occupancy_status: "empty",
    map_position: { left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 3, z_index: 0 },
    pallet: null,
    loose_items: []
  };

  const [mapped] = buildMappedLocationPallets(features, [location], "3F", STANDARD_PALLET, "layout-3f");
  assert.equal(mapped.zone_id, "zone-a1-current");
  assert.ok(mapped.x_mm > 10000);
  assert.deepEqual(
    buildMappedLocationPallets(features, [{ ...location, map_feature_id: "zone-removed" }], "3F", STANDARD_PALLET, "layout-3f"),
    []
  );
  assert.deepEqual(
    buildMappedLocationPallets(features, [{ ...location, map_feature_id: null }], "3F", STANDARD_PALLET, "layout-3f"),
    []
  );
});

test("only legacy V11 locations may fall back from map feature id to the current area zone", () => {
  const zone = {
    id: "zone-a1-current",
    feature_kind: "zone",
    feature_code: "ZONE-A1-CURRENT",
    erp_area_code: "A1",
    points: [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]
  };
  const location = {
    location_id: 82,
    location_code: "A1-L082",
    location_name: "V11兼容点位",
    floor_code: "3F",
    area_code: "A1",
    source_version: "V11",
    map_feature_id: null,
    position_status: "mapped",
    occupancy_status: "empty",
    map_position: { left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 3, z_index: 0 },
    pallet: null,
    loose_items: []
  };

  const [mapped] = buildMappedLocationPallets([zone], [location], "3F", STANDARD_PALLET, "layout-3f");
  assert.equal(mapped.zone_id, "zone-a1-current");
  assert.deepEqual(
    buildMappedLocationPallets([zone], [{ ...location, area_code: null }], "3F", STANDARD_PALLET, "layout-3f"),
    []
  );
  assert.deepEqual(
    buildMappedLocationPallets([zone], [{ ...location, source_version: "TWIN_V1" }], "3F", STANDARD_PALLET, "layout-3f"),
    []
  );
});

test("mapped locations use area-relative layout coordinates and convert 2D drags back to percentages", () => {
  const zone = {id: "zone-a1", feature_kind: "zone", feature_code: "ZONE-A1", erp_area_code: "A1", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]};
  const location = {
    location_id: 21, location_code: "A1-L01", location_name: "A1第一位", floor_code: "3F", area_code: "A1",
    source_version: "TWIN_V1", map_feature_id: "zone-a1",
    position_status: "mapped", occupancy_status: "empty", pallet: null, loose_items: [],
    map_position: {left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 7, z_index: 0}
  };
  const [pallet] = buildMappedLocationPallets([zone], [location], "3F", STANDARD_PALLET, "layout-3f");
  assert.equal(pallet.x_mm, 2500);
  assert.equal(pallet.y_mm, 4500);
  assert.equal(pallet.width_mm, 0);
  assert.equal(pallet.depth_mm, 0);
  assert.equal(pallet.height_mm, 0);
  assert.equal(pallet.is_logical_anchor, true);
  assert.equal(pallet.visual_kind, "location_anchor");
  assert.equal(pallet.display_label, "A1第一位");
  assert.doesNotMatch(pallet.status_note, /栈板/);
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

test("location planning keeps published zone geometry when an administrator draft was moved", () => {
  const published = [{
    id: "zone-d1",
    feature_kind: "zone",
    feature_code: "ZONE-D1",
    erp_area_code: "D1",
    points: [[1000, 0], [3550, 0], [3550, 15000], [1000, 15000]],
    version: 2
  }];
  const draft = [{
    ...published[0],
    points: [[1300, 900], [3850, 900], [3850, 15900], [1300, 15900]],
    version: 23,
    name: "D1 草稿名称"
  }];
  const [projected] = mergePublishedFeatureGeometry(draft, published);
  assert.deepEqual(projected.points, published[0].points);
  assert.equal(projected.version, 2);
  assert.equal(projected.name, "D1 草稿名称");
  assert.deepEqual(mergePublishedFeatureGeometry(draft, []), []);
  assert.deepEqual(
    mergePublishedFeatureGeometry([...draft, {...draft[0], id: "draft-only"}], published).map((item) => item.id),
    ["zone-d1"]
  );

  const location = {
    location_id: 151,
    map_position: {left_pct: 0, top_pct: 0, width_pct: 47.0588, height_pct: 6.6667, version: 4}
  };
  const geometry = locationLayoutGeometry(projected, location, 2800, 7500);
  assert.deepEqual(geometry, {
    location_id: 151,
    expected_version: 4,
    left_pct: 47.0588,
    top_pct: 46.6666,
    width_pct: 47.0588,
    height_pct: 6.6667,
    z_index: 0
  });
});

test("mapped pallet rotation follows each measured slot orientation", () => {
  const zone = {id: "zone-fin", feature_kind: "zone", feature_code: "ZONE-1F-FIN", erp_area_code: "FIN", points: [[0, 0], [2400, 0], [2400, 5000], [0, 5000]]};
  const base = {location_code: "FIN-L001", location_name: "成品位", floor_code: "1F", area_code: "FIN", source_version: "TWIN_V1", map_feature_id: "zone-fin", position_status: "mapped", occupancy_status: "occupied", pallet: {pallet_code: "PLT-FIN", items: [{lot_id: 1, quantity: 1}]}, loose_items: []};
  const normal = {...base, location_id: 31, map_position: {left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 1, z_index: 0}};
  const rotated = {...base, location_id: 32, location_code: "FIN-L002", map_position: {left_pct: 0, top_pct: 20, width_pct: 41.6667, height_pct: 24, version: 1, z_index: 0}};

  const pallets = buildMappedLocationPallets([zone], [normal, rotated], "1F", STANDARD_PALLET, "layout-1f");
  assert.equal(pallets[0].rotation_deg, 0);
  assert.equal(pallets[1].rotation_deg, 90);
  assert.deepEqual(pallets.map((item) => [Math.round(item.width_mm), Math.round(item.depth_mm)]), [[1200, 1000], [1200, 1000]]);
});

test("physical pallet locations ignore legacy footprint sizes and use one backend standard", () => {
  const zone = {
    id: "zone-fin",
    feature_kind: "zone",
    feature_code: "ZONE-1F-FIN",
    erp_area_code: "FIN",
    points: [[0, 0], [12000, 0], [12000, 6000], [0, 6000]]
  };
  const base = {
    location_name: "成品位",
    floor_code: "1F",
    area_code: "FIN",
    source_version: "TWIN_V1",
    map_feature_id: "zone-fin",
    position_status: "mapped",
    occupancy_status: "occupied",
    pallet: { pallet_code: "ERP-PALLET", items: [{ lot_id: 1, quantity: 1 }] },
    loose_items: []
  };
  const locations = [
    {
      ...base,
      location_id: 61,
      location_code: "FIN-L061",
      map_position: { left_pct: 0, top_pct: 0, width_pct: 2, height_pct: 3, version: 1, z_index: 0, layout_kind: "physical_pallet" }
    },
    {
      ...base,
      location_id: 62,
      location_code: "FIN-L062",
      map_position: { left_pct: 20, top_pct: 0, width_pct: 40, height_pct: 50, version: 1, z_index: 0, layout_kind: "physical_pallet" }
    },
    {
      ...base,
      location_id: 63,
      location_code: "FIN-L063",
      map_position: { left_pct: 70, top_pct: 0, width_pct: 8.333333, height_pct: 20, version: 1, z_index: 0, layout_kind: "physical_pallet" }
    }
  ];
  const standard = {
    contract_version: "standard-pallet-v1",
    width_mm: 1200,
    depth_mm: 1000,
    height_mm: 150
  };

  const pallets = buildMappedLocationPallets([zone], locations, "1F", standard, "layout-1f");

  assert.deepEqual(
    pallets.map((item) => [item.width_mm, item.depth_mm, item.height_mm]),
    [[1200, 1000, 150], [1200, 1000, 150], [1200, 1000, 150]]
  );
  assert.ok(pallets.every((item) => item.visual_kind === "physical_pallet"));
  assert.ok(pallets.every((item) => item.display_label === "成品位"));
  assert.equal(new Set(pallets.map((item) => item.layout_id)).size, 1);
  assert.equal(pallets[0].layout_id, "layout-1f");
});

test("legacy dispatch inventory is never projected into an unrelated measured zone", () => {
  const features = [{
    id: "dispatch-zone",
    feature_kind: "zone",
    feature_code: "ZONE-1F-DISPATCH",
    subtype: "finished_wait_delivery",
    points: [[0, 0], [6000, 0], [6000, 3000], [0, 3000]]
  }];
  const dispatchLocation = {
    location_id: 900,
    location_code: "F1-DISPATCH-01",
    location_name: "一楼成品暂存区",
    pallets: [{ pallet_id: 7, pallet_code: "PAL-007", version: 2, items: [] }],
    loose_items: []
  };
  const standard = {
    contract_version: "standard-pallet-v1",
    width_mm: 1200,
    depth_mm: 1000,
    height_mm: 150
  };

  assert.deepEqual(
    buildMeasuredDispatchPallets(features, dispatchLocation, "1F", standard, "layout-1f"),
    []
  );
});

test("confirmed-capacity logical positions render as non-pallet anchors", () => {
  const zone = {id: "zone-a1", feature_kind: "zone", feature_code: "ZONE-A1", erp_area_code: "A1", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]};
  const location = {
    location_id: 41, location_code: "A1-L041", location_name: "A1逻辑位", floor_code: "3F", area_code: "A1",
    source_version: "TWIN_V1", map_feature_id: "zone-a1",
    position_status: "mapped", occupancy_status: "empty", pallet: null, loose_items: [],
    map_position: {left_pct: 10, top_pct: 10, width_pct: 2, height_pct: 4, version: 1, z_index: 0}
  };

  const [anchor] = buildMappedLocationPallets([zone], [location], "3F", STANDARD_PALLET, "layout-3f");
  assert.equal(anchor.width_mm, 0);
  assert.equal(anchor.depth_mm, 0);
  assert.equal(anchor.height_mm, 0);
  assert.equal(anchor.is_logical_anchor, true);
  assert.equal(anchor.visual_kind, "location_anchor");
  assert.equal(anchor.display_label, "A1逻辑位");
  assert.match(anchor.status_note, /逻辑位置标记/);
  assert.doesNotMatch(anchor.status_note, /栈板|容量/);
});

test("real system pallet identity wins over legacy footprint while empty locations remain anchors", () => {
  const zone = {id: "zone-a1", feature_kind: "zone", feature_code: "ZONE-A1", erp_area_code: "A1", points: [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]};
  const base = {
    location_code: "A1-L001", location_name: "A1点位", floor_code: "3F", area_code: "A1",
    source_version: "TWIN_V1", map_feature_id: "zone-a1",
    position_status: "mapped", occupancy_status: "empty", pallet: null, loose_items: []
  };
  const locations = [
    {...base, location_id: 51, location_code: "A1-L051", occupancy_status: "occupied", pallet: {pallet_code: "PLT-A1-051", items: [{lot_id: 51, quantity: 1}]}, map_position: {left_pct: 0, top_pct: 0, width_pct: 50, height_pct: 20, version: 1, z_index: 0, layout_kind: "logical_anchor"}},
    {...base, location_id: 52, location_code: "A1-L052", map_position: {left_pct: 50, top_pct: 0, width_pct: 12, height_pct: 20, version: 1, z_index: 0, layout_kind: "logical_anchor"}}
  ];

  const pallets = buildMappedLocationPallets([zone], locations, "3F", STANDARD_PALLET, "layout-3f");
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

test("empty planning slots use their visible pallet footprint for column conflicts", () => {
  const planningSlot = {
    id: "erp-location-151",
    x_mm: 0,
    y_mm: 0,
    width_mm: 0,
    depth_mm: 0,
    rotation_deg: 0,
    visual_kind: "location_anchor",
    is_logical_anchor: true,
    is_planning_location_slot: true,
    planning_slot_width_mm: 1200,
    planning_slot_depth_mm: 1000
  };
  const columns = [{
    id: "column-d1",
    feature_kind: "structure",
    subtype: "custom_column",
    points: [[-325, 0], [325, 0]],
    width_mm: 700
  }];
  assert.deepEqual(findPalletColumnConflicts([planningSlot], [], columns), [
    { pallet_id: "erp-location-151", column_id: "column-d1" }
  ]);
});

test("planning conflict preview includes aisles before the authoritative save", () => {
  const pallet = {
    id: "erp-location-153",
    x_mm: 2000,
    y_mm: 3000,
    width_mm: 1200,
    depth_mm: 1000,
    rotation_deg: 0
  };
  const aisle = {
    id: "aisle-d1",
    feature_kind: "aisle",
    points: [[2000, 0], [2000, 6000]],
    width_mm: 800
  };
  assert.deepEqual(findPalletColumnConflicts([pallet], [], [aisle]), [
    { pallet_id: "erp-location-153", column_id: "aisle-d1" }
  ]);
});

test("planning conflict preview marks both overlapping locations in the same area", () => {
  const conflicts = findPalletColumnConflicts([
    {
      id: "erp-location-1",
      zone_id: "zone-d1",
      x_mm: 1000,
      y_mm: 1000,
      width_mm: 1200,
      depth_mm: 1000,
      rotation_deg: 0
    },
    {
      id: "erp-location-2",
      zone_id: "zone-d1",
      x_mm: 1500,
      y_mm: 1000,
      width_mm: 1200,
      depth_mm: 1000,
      rotation_deg: 0
    },
    {
      id: "erp-location-3",
      zone_id: "zone-e1",
      x_mm: 1000,
      y_mm: 1000,
      width_mm: 1200,
      depth_mm: 1000,
      rotation_deg: 0
    }
  ]);

  assert.deepEqual(conflicts, [
    { pallet_id: "erp-location-1", column_id: "location:erp-location-2" },
    { pallet_id: "erp-location-2", column_id: "location:erp-location-1" }
  ]);
});

test("percentage quantisation does not merge edge-touching D1 locations", () => {
  const zone = {
    id: "zone-d1",
    feature_kind: "zone",
    points: [[0, 0], [2550, 0], [2550, 15000], [0, 15000]]
  };
  const first = {
    id: "erp-location-151",
    zone_id: "zone-d1",
    x_mm: 600,
    y_mm: 500,
    width_mm: 1200,
    depth_mm: 1000,
    rotation_deg: 0
  };
  const quantisedTouch = {
    ...first,
    id: "erp-location-152",
    x_mm: 1799.9994
  };
  assert.deepEqual(
    findPalletColumnConflicts([first, quantisedTouch], [], [zone]),
    []
  );

  const realOverlap = { ...quantisedTouch, x_mm: 1799.9 };
  assert.deepEqual(
    findPalletColumnConflicts([first, realOverlap], [], [zone]),
    [
      { pallet_id: "erp-location-151", column_id: "location:erp-location-152" },
      { pallet_id: "erp-location-152", column_id: "location:erp-location-151" }
    ]
  );
});

test("planning conflict preview rejects zone overflow and confirmed equipment", () => {
  const zone = {
    id: "zone-d1",
    feature_kind: "zone",
    points: [[0, 0], [3000, 0], [3000, 2000], [0, 2000]]
  };
  const outOfBounds = {
    id: "erp-location-11",
    zone_id: "zone-d1",
    x_mm: 300,
    y_mm: 1000,
    width_mm: 1200,
    depth_mm: 1000,
    rotation_deg: 0
  };
  const besideEquipment = {
    id: "erp-location-12",
    zone_id: "zone-d1",
    x_mm: 2100,
    y_mm: 1000,
    width_mm: 1200,
    depth_mm: 1000,
    rotation_deg: 0
  };
  const equipment = {
    id: "equipment-1",
    x_mm: 2100,
    y_mm: 1000,
    width_mm: 500,
    depth_mm: 500,
    rotation_deg: 0,
    is_confirmed: true
  };

  assert.deepEqual(
    findPalletColumnConflicts([outOfBounds, besideEquipment], [], [zone], 0, [equipment]),
    [
      { pallet_id: "erp-location-11", column_id: "zone-boundary:zone-d1" },
      { pallet_id: "erp-location-12", column_id: "equipment-1" }
    ]
  );
});

test("shared occupied ground locations keep a full planning footprint", () => {
  const zone = {
    id: "zone-d1",
    feature_kind: "zone",
    feature_code: "D1",
    erp_area_code: "D1",
    points: [[0, 0], [3000, 0], [3000, 2000], [0, 2000]]
  };
  const location = normalizeInventoryLocationProjection({
    location_id: 14,
    location_code: "3F-D01-P01-01",
    location_name: "三楼 D1区·第1排·1号位",
    floor_code: "3F",
    area_code: "D1",
    storage_type: "ground",
    map_feature_id: "zone-d1",
    position_status: "mapped",
    occupancy_status: "occupied",
    map_position: { left_pct: 20, top_pct: 25, width_pct: 40, height_pct: 50, version: 1, layout_kind: "physical_pallet" },
    pallets: [
      { pallet_id: 1, pallet_code: "PLT-1", items: [{ lot_id: 1, available_quantity: 1 }] },
      { pallet_id: 2, pallet_code: "PLT-2", items: [{ lot_id: 2, available_quantity: 1 }] }
    ],
    loose_items: []
  });
  const [planned] = buildMappedLocationPallets([zone], [location], "3F", STANDARD_PALLET, "layout-3f", true);

  assert.equal(planned.is_logical_anchor, true);
  assert.equal(planned.is_planning_location_slot, true);
  assert.equal(planned.planning_slot_width_mm, 1200);
  assert.equal(planned.planning_slot_depth_mm, 1000);
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
    source_version: "TWIN_V1",
    map_feature_id: "zone-dispatch",
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

  const mapped = buildMappedLocationPallets([zone], [shared], "1F", STANDARD_PALLET, "layout-1f");
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

test("legacy dispatch stock stays in the unlocated blocker instead of borrowing FIN geometry", () => {
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

  const first = buildMeasuredDispatchPallets(zones, dispatch, "1F", STANDARD_PALLET, "layout-1f");
  const second = buildMeasuredDispatchPallets(zones, dispatch, "1F", STANDARD_PALLET, "layout-1f");
  assert.deepEqual(first, second);
  assert.deepEqual(first, []);
  assert.deepEqual(buildMeasuredDispatchPallets(zones, dispatch, "3F", STANDARD_PALLET, "layout-3f"), []);
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
      product_name: "五层瓦楞纸箱",
      customer_id: 7,
      customer_name: "苏州思迈尔包装有限公司",
      customer_short_name: "思迈尔",
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
  assert.equal(first.candidate.customer_short_name, "思迈尔");
  assert.equal(first.candidate.inventory_code, "CP-11");
  assert.equal(first.candidate.product_name, "五层瓦楞纸箱");
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

test("merge suggestions use inventory code and specification instead of internal product id", () => {
  assert.equal(palletMergeSuggestionProductKey([
    { product_id: 11, inventory_code: "cpn-001", specification: "500×300", quantity: 3 },
    { product_id: 22, inventory_code: "CPN-001", specification: "500×300", quantity: 4 }
  ]), "CPN-001|500×300");
  assert.equal(palletMergeSuggestionProductKey([
    { inventory_code: "CPN-001", specification: "500×300", quantity: 3 },
    { inventory_code: "CPN-001", specification: "510×300", quantity: 4 }
  ]), null);
  assert.equal(palletMergeSuggestionProductKey([
    { inventory_code: "", specification: "500×300", quantity: 3 }
  ]), null);
});

test("merge suggestions can be narrowed by customer and multi-term inventory search", () => {
  const suggestion = {
    label: "CPN-001 · 500×300",
    candidates: [normalizePalletMergeCandidate(
      mergeLocation({ location_code: "A2-7", location_name: "右区A2 A2-7" }),
      mergePallet(18, { items: [{
        ...mergePallet(18).items[0],
        customer_id: 7,
        customer_name: "苏州思迈尔包装有限公司",
        customer_short_name: "思迈尔",
        inventory_code: "CPN-001",
        product_name: "五层加强纸箱",
        specification: "500×300"
      }] })
    ).candidate]
  };
  assert.equal(palletMergeSuggestionMatchesFilter(suggestion, "7", "CPN-001 500×300"), true);
  assert.equal(palletMergeSuggestionMatchesFilter(suggestion, "7", "思迈尔 加强"), true);
  assert.equal(palletMergeSuggestionMatchesFilter(suggestion, "7", "A2-7"), true);
  assert.equal(palletMergeSuggestionMatchesFilter(suggestion, "8", "CPN-001"), false);
  assert.equal(palletMergeSuggestionMatchesFilter(suggestion, "7", "CPN-002"), false);
});

test("stocktake drafts upsert by formal inventory identity and preserve the client item id", () => {
  const add = {
    client_item_id: "stocktake-add-1", operation: "add", location_id: 21, expected_layout_version: 3,
    location_code: "A1-01", location_name: "三楼 A1-01", floor_code: "3F", area_code: "A1",
    customer_id: 7, customer_name: "苏州思迈尔包装有限公司", product_id: 99,
    inventory_code: "CP-099", product_name: "五层加强纸箱", inventory_type: "finished",
    unit: "boxes", quantity: 20, stock_date: "2026-08-13"
  };
  const first = upsertStocktakeDraft([], add);
  const replacement = upsertStocktakeDraft(first.items, { ...add, client_item_id: "discarded", quantity: 25 });
  assert.equal(replacement.error, null);
  assert.equal(replacement.items.length, 1);
  assert.equal(replacement.items[0].client_item_id, "stocktake-add-1");
  assert.equal(replacement.items[0].quantity, 25);
  const conflict = upsertStocktakeDraft(replacement.items, { ...add, client_item_id: "semi", inventory_type: "semi_finished", unit: "sheets" });
  assert.match(conflict.error, /同一货位/);
  assert.deepEqual(conflict.items, replacement.items);
});

test("stocktake location gates reject unsupported floors and dispatch while preserving formal rack rules", () => {
  const ground = {
    location_code: "3F-A1-01", floor_code: "3F", area_code: "A1",
    warehouse_type: "finished", storage_type: "ground", is_active: true,
    position_status: "mapped", source_version: "TWIN_V1", map_position: { version: 3 }
  };
  assert.equal(stocktakeLocationBlockReason(ground), null);
  assert.equal(stocktakeAddBlockReason(ground, "finished"), null);
  assert.equal(stocktakeAddBlockReason({ ...ground, location_code: "4F-A1-01", floor_code: "4F" }, "finished"), null);
  assert.equal(stocktakeLocationBlockReason({ ...ground, floor_code: "1F" }), null);
  assert.equal(stocktakeLocationBlockReason({ ...ground, source_version: "CURRENT_MAP" }), null);
  assert.equal(stocktakeLocationBlockReason({ ...ground, source_version: "CURRENT_MAP", floor_code: "1F" }), null);
  assert.equal(stocktakeLocationBlockReason({ ...ground, source_version: "V11" }), null);
  assert.match(stocktakeLocationBlockReason({ ...ground, source_version: undefined }), /正式地图/);
  assert.match(stocktakeLocationBlockReason({ ...ground, source_version: "twin_v1" }), /正式地图/);
  assert.match(stocktakeLocationBlockReason({ ...ground, source_version: " TWIN_V1 " }), /正式地图/);
  assert.match(stocktakeLocationBlockReason({ ...ground, floor_code: "3f" }), /正式地图/);
  assert.match(stocktakeLocationBlockReason({ ...ground, floor_code: " 3F " }), /正式地图/);
  assert.match(stocktakeLocationBlockReason({ ...ground, source_version: "V11", floor_code: "1F" }), /正式地图/);
  assert.match(stocktakeLocationBlockReason({ ...ground, floor_code: "2F" }), /正式地图/);
  assert.match(stocktakeLocationBlockReason({ ...ground, area_code: null }), /正式区域或库位编码/);
  assert.match(stocktakeLocationBlockReason({ ...ground, location_code: null }), /正式区域或库位编码/);
  assert.match(stocktakeLocationBlockReason({ ...ground, area_code: "dispatch" }), /待送区/);
  assert.match(stocktakeLocationBlockReason({ ...ground, area_code: "A1", location_code: "1F-DISPATCH-01" }), /待送区/);
  assert.match(stocktakeLocationBlockReason({ ...ground, is_active: false }), /停用/);
  assert.match(stocktakeLocationBlockReason({ ...ground, position_status: "unplaced" }), /正式地图/);

  const rack = { ...ground, location_code: "3F-F1-R01", storage_type: "rack" };
  assert.equal(stocktakeAddBlockReason(rack, "finished"), null);
  assert.equal(stocktakeAddBlockReason({ ...rack, warehouse_type: "semi_finished" }, "semi_finished"), null);
});

test("stocktake blocked goods include a concrete recovery path", () => {
  assert.match(stocktakeBlockResolution("库存仍有待送分配预占"), /释放占用/);
  assert.match(stocktakeBlockResolution("该库存处于冻结状态"), /解冻/);
  assert.match(stocktakeBlockResolution("该货位尚未接入可盘点的正式地图"), /区域规划.*货位发布/);
  assert.match(stocktakeBlockResolution("当前仍有未完成盘点任务"), /完成或撤销/);
});

test("stocktake product lookup finds exact existing stock and sorts other areas first", () => {
  const item = {
    lot_id: 71, version: 4, inventory_type: "finished", unit: "boxes",
    customer_id: 7, product_id: 99, inventory_code: "CP-099", product_name: "五层箱",
    available_quantity: 12
  };
  const locations = [
    {
      location_id: 21, floor_code: "3F", area_code: "A1", location_code: "A1-01",
      location_name: "A1-01", map_position: { version: 3 }, loose_items: [{ ...item, lot_id: 72, available_quantity: 5 }]
    },
    {
      location_id: 31, floor_code: "1F", area_code: "B2", location_code: "B2-01",
      employee_location_name: "一楼 B2-01", map_position: { version: 6 },
      pallets: [{ items: [item, { ...item, lot_id: 73, product_id: 100 }] }]
    },
    {
      location_id: 41, floor_code: "3F", area_code: "C1", location_code: "C1-01",
      location_name: "C1-01", map_position: { version: 2 },
      loose_items: [{ ...item, lot_id: 74, inventory_type: "semi_finished", product_id: null, allowed_product_ids: [99], unit: "sheets", available_quantity: 20 }]
    }
  ];
  const finished = stocktakeExistingProductLocations(locations, {
    customerId: 7, productId: 99, inventoryType: "finished", targetFloorCode: "3F", targetAreaCode: "A1", targetLocationId: 21
  });
  assert.deepEqual(finished.map((row) => row.lot_id), [71, 72]);
  assert.equal(finished[0].is_outside_target_area, true);
  assert.equal(finished[0].source_location_name, "一楼 B2-01");
  assert.equal(finished[1].is_target_location, true);
  const semi = stocktakeExistingProductLocations(locations, {
    customerId: 7, productId: 99, inventoryType: "semi_finished", targetAreaCode: "A1"
  });
  assert.deepEqual(semi.map((row) => row.lot_id), [74]);
});

test("stocktake decrease eligibility fails closed and preserves the backend block reason", () => {
  assert.equal(stocktakeDecreaseBlockReason({ stocktake_decrease_eligible: true, stocktake_decrease_block_reason: null }), null);
  assert.equal(
    stocktakeDecreaseBlockReason({ stocktake_decrease_eligible: false, stocktake_decrease_block_reason: "库存仍绑定生产任务" }),
    "库存仍绑定生产任务"
  );
  assert.match(stocktakeDecreaseBlockReason({}), /未通过/);
});

test("stocktake draft validation fixes units and protects available quantity", () => {
  const invalidUnit = {
    client_item_id: "bad-unit", operation: "add", location_id: 1, expected_layout_version: 3, customer_id: 2, product_id: 3,
    inventory_type: "finished", unit: "sheets", quantity: 1, stock_date: "2026-08-13"
  };
  assert.match(validateStocktakeDraft(invalidUnit), /boxes/);
  const decrease = {
    client_item_id: "dec-1", operation: "decrease", location_id: 21, expected_layout_version: 3, lot_id: 88,
    expected_version: 4, quantity: 11, available_quantity: 10
  };
  assert.match(validateStocktakeDraft(decrease), /可用数量/);
});

test("stocktake batch strips display fields and never emits remove semantics", () => {
  const add = {
    client_item_id: "add-1", operation: "add", location_id: 21, expected_layout_version: 3, location_code: "A1-01",
    location_name: "三楼 A1-01", floor_code: "3F", area_code: "A1", customer_id: 7,
    customer_name: "苏州思迈尔包装有限公司", product_id: 99, inventory_code: "CP-099",
    product_name: "五层加强纸箱", inventory_type: "finished", unit: "boxes", quantity: 20,
    stock_date: "2026-08-13"
  };
  const decrease = {
    client_item_id: "dec-1", operation: "decrease", location_id: 21, expected_layout_version: 3, location_code: "A1-01",
    location_name: "三楼 A1-01", floor_code: "3F", area_code: "A1", lot_id: 88,
    expected_version: 4, customer_name: "苏州思迈尔包装有限公司", inventory_code: "CP-099",
    product_name: "五层加强纸箱", unit: "boxes", quantity: 10, available_quantity: 10,
    quantity_before: 10
  };
  const payload = buildStocktakeBatchPayload("stocktake-batch-key", [add, decrease]);
  assert.deepEqual(payload, {
    idempotency_key: "stocktake-batch-key", confirmed: true,
    items: [
      { client_item_id: "add-1", operation: "add", location_id: 21, expected_layout_version: 3, customer_id: 7, product_id: 99, inventory_type: "finished", unit: "boxes", quantity: 20, stock_date: "2026-08-13", source_kind: "existing_stocktake" },
      { client_item_id: "dec-1", operation: "decrease", location_id: 21, expected_layout_version: 3, lot_id: 88, expected_version: 4, quantity: 10 }
    ]
  });
  assert.equal(JSON.stringify(payload).includes("remove"), false);
  assert.deepEqual(removeStocktakeDraft([add, decrease], "add-1"), [decrease]);
  assert.deepEqual(clearStocktakeDrafts(), []);
});

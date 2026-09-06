import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import * as inventory from "../src/warehouseInventory.mjs";

const source = readFileSync(new URL("../src/WarehouseTwinApp.tsx", import.meta.url), "utf8");
const editorSource = readFileSync(new URL("../src/EditorCanvas.tsx", import.meta.url), "utf8");
const sceneSource = readFileSync(new URL("../src/industrialScene.ts", import.meta.url), "utf8");
const inventorySource = readFileSync(new URL("../src/warehouseInventory.mjs", import.meta.url), "utf8");
const cssSource = readFileSync(new URL("../src/warehouseTwin.css", import.meta.url), "utf8");

// Execute the component's actual selectors/handlers without mounting a second UI.
// This catches wiring errors which tests of the projection utility alone miss.
const syntax = ts.createSourceFile("WarehouseTwinApp.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function componentValue(name, context, optional = false) {
  let initializer;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(syntax) === name) initializer = node.initializer;
    ts.forEachChild(node, visit);
  }
  visit(syntax);
  if (!initializer && optional) return undefined;
  assert.ok(initializer, `component declaration ${name}`);
  const javascript = ts.transpileModule(`globalThis.value = (${initializer.getText(syntax)});`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None }
  }).outputText;
  const sandbox = { ...context };
  vm.runInNewContext(javascript, sandbox);
  return sandbox.value;
}

test("scoped geometry apply keeps its retry key and distinguishes commit from readback", async () => {
  for (const stage of ["write-fails", "read-fails", "success"]) {
    const calls = [], messages = [], modes = [];
    const requestRef = {current:null};
    const context = {
      layout:{source_sha256:"saved-draft"}, selectedAreaFeature:{id:"zone-d1",version:7},
      spatialEditBusy:false, locationEditBusy:false, activeLocationDraftCount:0,
      zoneGeometryDrafts:{},rackDrafts:{},zonePolicyDrafts:{}, floorCode:"3F",
      planningPublishedLayout:{source_sha256:"published-before"},publishedFloorRevision:"published-before",
      geometryApplyRequestRef:requestRef,operationKey:()=>"stable-zone-apply-key",
      setSpatialEditBusy:()=>{}, setLocationEditMessage:m=>messages.push(m),
      mutateJson:async(url,method,payload)=>{calls.push({url,method,payload});if(stage==="write-fails")throw Error("network");return {applied:true};},
      refreshPlanningTwinFloor:async()=>{if(stage==="read-fails")throw Error("readback");},
      refreshDashboard:async()=>{},setMapMode:m=>modes.push(m),setSearchPanelOpen:()=>{},
      setLocationEditMode:()=>{},setAreaPolicyEditMode:()=>{},setAdvancedAreaMaintenanceOpen:()=>{},
      setLocationPointEditAreaCode:()=>{},setLayoutMapToolsOpen:()=>{},
    };
    context.applySavedAreaGeometryRevision = componentValue("applySavedAreaGeometryRevision", context);
    const handler = componentValue("applySelectedAreaGeometry",context);
    await handler();
    assert.equal(calls[0].url,"/api/warehouse/twin-layout/floors/3F/zones/zone-d1/apply-geometry");
    assert.equal(calls[0].payload.expected_revision,"saved-draft");
    if(stage==="success") {assert.deepEqual(modes,["lookup"]);assert.equal(requestRef.current,null);}
    else {assert.deepEqual(modes,[]);assert(requestRef.current);await handler();assert.equal(calls[0].payload.operation_key,calls[1].payload.operation_key);}
    assert.match(messages[0],stage==="write-fails"?/尚未应用/:stage==="read-fails"?/已应用.*回读未完成/:/已应用.*已刷新/);
  }
});

test("stocktake keeps an acknowledged receipt when readback fails and retains only unacknowledged drafts", async () => {
  for (const acknowledged of [false, true]) {
    const state = { drafts: [{ operation: "add", quantity: 1 }], key: "same-request", receipt: [], refresh: false, calls: 0, message: "" };
    const context = {
      stocktakeDrafts: state.drafts, stocktakeBatchBusy: false, stocktakeRefreshRequired: false,
      stocktakeBatchIdempotencyKey: state.key, window: { confirm: () => true }, inventoryUnitLabel: () => "箱",
      buildStocktakeBatchPayload: key => ({ idempotency_key: key }),
      mutateJson: async () => { state.calls++; if (!acknowledged) throw Error("write response unavailable"); return { items: [{ operation: "add", lot_id: 7 }] }; },
      refreshDashboard: async () => { throw Error("read unavailable"); }, clearStocktakeDrafts: () => [], operationKey: () => "next-request",
      setStocktakeBatchBusy: () => {}, setStocktakeDrafts: v => { state.drafts = v; },
      setStocktakeLastResult: v => { state.receipt = v; }, setStocktakeLotId: () => {}, setStocktakeDecreaseQuantity: () => {},
      setStocktakeBatchIdempotencyKey: v => { state.key = v; }, setStocktakeRefreshRequired: v => { state.refresh = v; },
      setWarehouseOperationMessage: v => { state.message = v; }, stocktakeBlockResolution: v => v,
    };
    await componentValue("confirmStocktakeDrafts", context)();
    assert.equal(state.drafts.length, acknowledged ? 0 : 1);
    assert.equal(state.receipt.length, acknowledged ? 1 : 0);
    assert.equal(state.key, acknowledged ? "next-request" : "same-request");
    assert.equal(state.refresh, acknowledged);
    assert.match(state.message, acknowledged ? /已写入.*刷新失败/ : /结果未确认/);
    if (acknowledged) {
      await componentValue("confirmStocktakeDrafts", { ...context, stocktakeDrafts: state.drafts, stocktakeRefreshRequired: state.refresh })();
      assert.equal(state.calls, 1);
    }
  }
});

test("new racks use entered dimensions and explicit cells for three and four levels", async () => {
  for (const levels of [3, 4]) {
    const calls = [], messages = [];
    const context = {
      layout: { source_sha256: "draft" }, selectedAreaFeature: { id: "area" }, spatialEditBusy: false,
      newRackSettings: { width: "1200", depth: "500", height: "2000", levels: String(levels), cells: "2" },
      featureCenter: () => ({ x: 200, y: 300 }), featureAreaCode: () => "C4", floorCode: "3F",
      setSpatialEditBusy: () => {}, operationKey: () => "rack-new",
      mutateJson: async (url, method, payload) => { calls.push({ url, method, payload }); return { revision: "saved", item: { id: "new", rack_code: "stable" } }; },
      setLayout: () => {}, rememberServerDraft: () => {}, setRackDrafts: () => {}, rackDraft: r => r,
      setSelected: () => {}, setNewRackFormOpen: () => {}, setLocationEditMessage: v => messages.push(v),
    };
    await componentValue("addRackToSelectedArea", context)();
    assert.equal(calls.length, 1);
    const p = calls[0].payload;
    assert.equal(p.width_mm, 1200); assert.equal(p.depth_mm, 500); assert.equal(p.height_mm, 2000);
    assert.equal(p.levels, levels); assert.deepEqual(Array.from(p.level_cell_counts), Array(levels).fill(2));
    assert.equal(p.level_heights_mm.length, levels - 1);
    assert.equal(p.area_feature_id, "area"); assert.match(messages.at(-1), /草稿/);
    calls.length = 0; context.newRackSettings.cells = "";
    await componentValue("addRackToSelectedArea", context)();
    assert.equal(calls.length, 0); assert.match(messages.at(-1), /实际/);
  }
});

test("rack save applies only the current rack and reloads the operational map", async () => {
  const calls = [], messages = [];
  const rack = { id: "rack-1", name: "C1 01号架", version: 1, levels: 3, level_cell_counts: [4,4,4] };
  const context = {
    layout: { source_sha256: "draft-before", racks: [rack] }, spatialEditBusy: false,
    planningPublishedLayout: { source_sha256: "published-before" }, publishedFloorRevision: "published-before",
    floorCode: "3F", rackApplyRequestRef: { current: null }, operationKey: name => `stable-${name}`,
    rackMutationPayload: value => value, setSpatialEditBusy: () => {}, rememberServerDraft: () => {},
    setLayout: () => {}, setRackDrafts: () => {}, setLocationEditMessage: value => messages.push(value),
    mutateJson: async (url, method, payload) => {
      calls.push({ url, method, payload });
      if (method === "PATCH") return { revision: "draft-saved", item: { ...rack, version: 2 } };
      return { applied: true, scope: "rack_layout" };
    },
    refreshPlanningTwinFloor: async () => {}, refreshDashboard: async () => {},
  };
  context.applySavedRackRevision = componentValue("applySavedRackRevision", context);
  await componentValue("saveRackDraftImmediately", context)(rack.id, rack);
  assert.equal(calls.length, 2);
  assert.match(calls[0].url, /racks\/rack-1$/);
  assert.equal(calls[0].method, "PATCH");
  assert.match(calls[1].url, /racks\/rack-1\/apply$/);
  assert.equal(calls[1].payload.expected_published_revision, "published-before");
  assert.equal(context.rackApplyRequestRef.current, null);
  assert.match(messages.at(-1), /已保存并应用.*三种模式/);
  assert.doesNotMatch(source, /保存到草稿<\/button>/);
  assert.match(source, />保存并应用货架<\/button>/);
});

test("area numbering saves only selected racks and blocks unsaved dimension changes", async () => {
  let layout = { source_sha256: "before", racks: [{ id: "a", name: "old", width_mm: 1200 }, { id: "other", name: "keep" }] };
  const calls = [], context = {
    layout, selectedAreaFeature: { id: "area" }, spatialEditBusy: false, selectedAreaRacks: [layout.racks[0]], rackDrafts: {},
    rackMutationPayload: r => r, rackDraft: r => r, floorCode: "3F", operationKey: () => "number-racks",
    setSpatialEditBusy: () => {}, setLocationEditMessage: () => {}, rememberServerDraft: () => {},
    setLayout: fn => { layout = fn(layout); }, setRackDrafts: () => {},
    mutateJson: async (url, method, payload) => { calls.push({ url, method, payload }); return { revision: "after", item: { racks: [{ id: "a", name: "Area 01号架", width_mm: 1200 }] } }; },
  };
  await componentValue("numberSelectedAreaRacks", context)();
  assert.equal(calls.length, 1); assert.match(calls[0].url, /zones\/area\/number-racks$/);
  assert.equal(layout.racks[1].name, "keep"); assert.equal(layout.racks[0].id, "a");
  calls.length = 0; context.rackDrafts = { a: { ...context.layout.racks[0], width_mm: 1400 } };
  await componentValue("numberSelectedAreaRacks", context)(); assert.equal(calls.length, 0);
});

test("both map publish entrances reload warehouse records before claiming the new map is ready", async () => {
  for (const handler of ["publishLayoutDraft", "previewAndPublishLayout"]) {
    for (const readFails of [false, true]) {
      const steps = [], messages = [];
      const context = {
        layout: { source_sha256: "new-map" }, floorCode: "3F",
        layoutDraftControl: { status: "validated", has_draft: true, published_revision: "old-map", warnings: [] },
        window: { confirm: () => true }, operationKey: () => "publish-request",
        prepareLegacyRackBindingConfirmation: async () => ({ request: {}, summary: "" }),
        mutateJson: async url => url.endsWith("/validate") ? { status: "validated", draft_revision: "new-map", blockers: [], warnings: [] } : { backup_name: "previous-map.json" },
        refreshPublishedTwinFloor: async () => { steps.push("layout"); },
        refreshDashboard: async () => { steps.push("warehouse"); if (readFails) throw Error("warehouse readback unavailable"); },
        setSpatialEditBusy: () => {}, setLayoutDraftControl: () => {}, setLocationEditMessage: v => { messages.push(v); },
        setMapMode: () => {}, setSearchPanelOpen: () => {}, setLocationEditMode: () => {}, setAreaPolicyEditMode: () => {},
        setAdvancedAreaMaintenanceOpen: () => {}, setRackDrafts: () => {}, setZonePolicyDrafts: () => {}, replaceZoneGeometryDrafts: () => {},
        setLegacyRackBindingPreview: () => {}, setLegacyRackBindingSelections: () => {},
      };
      await componentValue(handler, context)();
      assert.deepEqual(steps, ["layout", "warehouse"]);
      assert.match(messages.at(-1), readFails ? /已发布.*回读失败/ : /已发布.*旧地图备份/);
      if (readFails) assert.doesNotMatch(messages.at(-1), /发布布局失败/);
    }
  }
});

test("a rejected pallet cannot become a move source even when it has a version", () => {
  const declaration = syntax.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "palletMoveSource");
  assert.ok(declaration);
  const javascript = ts.transpileModule(declaration.getText(syntax) + "\nglobalThis.make = palletMoveSource;", {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None }
  }).outputText;
  const sandbox = { employeeCustomerName: () => "客户", singleLocationPallet: location => location.pallets[0] };
  vm.runInNewContext(javascript, sandbox);
  const pallet = { pallet_id: 3, version: 2, items: [{}], move_eligible: false, move_block_reason: "历史关联未通过整板核验" };
  const location = { location_id: 1, floor_code: "3F", pallets: [pallet] };
  assert.equal(sandbox.make(location, pallet), null);
  assert.equal(sandbox.make(location), null);
  assert.equal(sandbox.make(location, { ...pallet, move_eligible: true }).pallet_id, 3);
  assert.equal(sandbox.make(location, { ...pallet, move_eligible: true, version: 0 }), null);
});

test("pending placement separates acknowledged write from refresh failure and blocks another submit", async () => {
  const calls = [], changes = [];
  const context = {
    pendingPlacementRef: { current: { busy: false, signature: "", key: "" } },
    pendingRefreshRequired: false, canEditLocations: true,
    selectedLocation: { location_id: 10, map_position: { version: 2 } },
    selectedPendingItem: { lot_id: 8, version: 3, available_quantity: 10, unit: "boxes" },
    selectedLocationFinishedAddBlockReason: null, pendingQuantity: "4",
    movableLotQuantity: item => item.available_quantity,
    operationKey: () => "pending-key", employeeLocationName: () => "目标货位", inventoryUnitLabel: () => "箱",
    mutateJson: async (...args) => { calls.push(args); },
    refreshDashboard: async () => { throw new Error("回读中断"); },
    setPendingPlacementBusy: () => {}, setPendingRefreshRequired: value => { context.pendingRefreshRequired = value; },
    setRecountLotId: value => changes.push(value), setPendingQuantity: value => changes.push(value),
    setWarehouseOperationMessage: value => changes.push(value),
  };
  await componentValue("placePendingInventory", context)();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2].quantity, 4);
  assert.equal(context.pendingRefreshRequired, true);
  assert(changes.some(value => typeof value === "string" && value.includes("货物已归位，但地图刷新失败")));
  await componentValue("placePendingInventory", context)();
  assert.equal(calls.length, 1);
});

test("object context menu only selects a known card without choosing a source or target", () => {
  for (const mode of ["lookup", "move", "planning"]) {
    const changes = [];
    const trigger = {};
    const handler = componentValue("openObjectActions", {
      mapMode: mode, locationEditMode: false, spatialEditBusy: false, loading: false,
      features: [{ id: "area-1", feature_kind: "zone" }], layout: { racks: [] },
      visualLocations: [{ location_id: 10 }], dispatchStagingPallets: [],
      objectActionTriggerRef: { current: null },
      document: { querySelector: () => trigger },
      setSelected: value => changes.push(["selected", value.id]),
      setObjectActions: value => changes.push(["menu", value.entity.id]),
    });
    assert.equal(handler({ kind: "pallet", id: "unknown" }, 100, 100), false);
    assert.equal(changes.length, 0);
    assert.equal(handler({ kind: "pallet", id: "erp-location-10" }, 100, 100), mode !== "planning");
    assert.deepEqual(changes, mode === "planning" ? [] : [["selected", "erp-location-10"], ["menu", "erp-location-10"]]);
  }
});

test("object operations preserve explicit source selection and respect failed mode entry and permissions", async () => {
  for (const action of ["relocate", "stocktake"]) {
    for (const permitted of [false, true]) {
      for (const entered of [false, true]) {
        const changes = [];
        let entries = 0;
        await componentValue("runObjectAction", {
          objectActions: { entity: { kind: "pallet", id: "erp-location-10" } },
          spatialEditBusy: false, loading: false, mapMode: "lookup", locationEditMode: false,
          traceReadOnly: false, canExecuteWarehouse: permitted, canStocktake: permitted,
          enterWarehouseMoveMode: async () => { entries++; return entered; },
          setMoveAction: value => changes.push(["mode", value]),
          setMoveSource: value => { assert.equal(value, null); changes.push(["source", null]); },
          setMoveQuantity: value => assert.equal(value, ""),
          setMoveDraftTargetLocationId: value => assert.equal(value, ""),
          setWarehouseOperationMessage: value => assert.match(value, /尚未/),
          setSelected: value => changes.push(["selected", value.id]),
          closeObjectActions: () => changes.push(["closed"]),
          requestAnimationFrame: () => {},
        })(action);
        assert.equal(entries, permitted ? 1 : 0);
        assert.deepEqual(changes, permitted && entered ? [["mode", action], ["source", null], ["selected", "erp-location-10"], ["closed"]] : []);
      }
    }
  }
});

test("acknowledged warehouse moves are not offered again when dashboard refresh fails", async () => {
  for (const failure of ["write", "readback", null]) {
    let drafts = [{ client_item_id: "move-one", source_location_id: 1, target_location_id: 2 }];
    let source = { source_key: "pallet:1" };
    let busy = false;
    const messages = [];
    const key = "same-request";
    let activeKey = key;
    let reads = 0;
    await componentValue("confirmMoveDrafts", {
      moveDrafts: drafts, moveBatchBusy: false, moveBatchIdempotencyKey: key,
      setMoveBatchBusy: value => { busy = value; },
      setWarehouseOperationMessage: value => messages.push(value),
      buildMoveBatchPayload: (idempotencyKey, items) => ({ idempotencyKey, items }),
      mutateJson: async (_url, _method, payload) => {
        assert.equal(payload.idempotencyKey, key);
        assert.equal(payload.items.length, 1);
        if (failure === "write") throw new Error("写入未确认");
        return { applied: true };
      },
      refreshDashboard: async () => { reads++; if (failure === "readback") throw new Error("刷新中断"); },
      setMoveDrafts: value => { drafts = value; },
      setMoveSource: value => { source = value; },
      setMoveQuantity: () => {}, setMoveDraftTargetLocationId: () => {},
      setMoveBatchIdempotencyKey: value => { activeKey = value; },
      operationKey: () => "next-request"
    })();
    assert.equal(busy, false);
    if (failure === "write") {
      assert.equal(drafts.length, 1);
      assert.equal(source.source_key, "pallet:1");
      assert.equal(activeKey, key, "uncertain write retains idempotency protection");
      assert.equal(reads, 0);
    } else {
      assert.equal(drafts.length, 0, "acknowledged move is no longer a pending preview");
      assert.equal(source, null);
      assert.equal(activeKey, "next-request");
      assert.equal(reads, 1);
      assert.match(messages.at(-1), failure === "readback" ? /移货已完成.*刷新失败.*不要重复提交/ : /移货已成功/);
    }
  }
});

test("moving a draft boundary preserves real location coordinates and keeps locations editable", () => {
  const zone = { id: "zone-map-test", feature_code: "ZONE-3F-TEST", feature_kind: "zone", erp_area_code: "TEST", version: 1,
    points: [[0, 0], [6000, 0], [6000, 4000], [0, 4000]] };
  const empty = { ...zone, id: "zone-empty", erp_area_code: "EMPTY", points: [[0, 5000], [3000, 5000], [3000, 7000], [0, 7000]] };
  const aisle = { id: "aisle-test", feature_code: "AISLE-TEST", feature_kind: "aisle", points: [[7000, 0], [7000, 8000]], width_mm: 1000 };
  const draft = { ...zone, version: 2, points: [[10000, 1000], [16000, 1000], [16000, 5000], [10000, 5000]] };
  const locations = [{ location_id: 151, location_code: "TEST-01", floor_code: "3F", area_code: "TEST", source_version: "TWIN_V1", storage_type: "ground", map_feature_id: zone.id,
    occupancy_status: "occupied", position_status: "mapped", map_position: { left_pct: 10, top_pct: 10, width_pct: 20, height_pct: 25, version: 4, z_index: 0 },
    pallet: null, loose_items: [{ lot_id: 951, quantity: 100, quantity_available: 100, inventory_type: "semi_finished", unit: "sheets" }] }];
  const original = JSON.stringify(locations);
  const published = [zone, empty, aisle];
  function render(mode, localPoints = null, preview = mode === "planning") {
    const features = mode === "planning" ? [draft, empty, aisle] : published;
    const context = { ...inventory, useMemo: (callback) => callback(), EMPTY_CANVAS_IDS: componentValue("EMPTY_CANVAS_IDS", {}), mapMode: mode, features,
      planningPreviewActive: preview, activeEditingFeatureId: preview ? zone.id : null,
      displayBaseLayout: { features: published, racks: [] },
      stableTwinFeatures: (layout) => layout.features, planningCollisionRacks: [],
      planningPublishedFeatures: published, zoneGeometryDrafts: localPoints ? { [zone.id]: localPoints } : {},
      visualLocations: locations, floorCode: "3F", standardPallet: { contract_version: "standard-pallet-v1", width_mm: 1200, depth_mm: 1000, height_mm: 150 },
      layout: { id: "test-3f", features, racks: [], violations: [] }, locationEditMode: mode === "planning", locationPointEditAreaCode: null,
      moveDrafts: [], moveAction: "pallet", groundCandidates: null, groundPrimaryLocationId: null, groundSecondaryLocationId: null,
      rackDrafts: {}, displayedLocationConflicts: [] };
    for (const name of ["locationProjectionFeatures", "planningVisibleFeatures", "mappedLocationPallets", "planningPreviewPallets", "previewOnlyLocationIds", "locationPointEditPalletIds", "movePreviewPallets", "groundCandidatePallets", "visualLayout"]) {
      const value = componentValue(name, context);
      if (value !== undefined) context[name] = value;
    }
    return context;
  }
  const lookup = render("lookup");
  const planning = render("planning");
  const browsing = render("planning", null, false);
  assert.deepEqual(JSON.parse(JSON.stringify(browsing.visualLayout.features)), JSON.parse(JSON.stringify(lookup.visualLayout.features)), "ordinary planning uses the same published geometry as lookup even with a saved draft");
  const before = lookup.visualLayout.pallets[0];
  const after = planning.visualLayout.pallets[0];
  assert.ok(before && after, "the same real inventory remains visible");
  assert.equal(after.id, before.id);
  assert.equal(planning.previewOnlyLocationIds.has(after.id), false);
  assert.equal(planning.locationPointEditPalletIds.includes(after.id), true, "locations remain editable after moving the boundary");
  assert.equal(lookup.previewOnlyLocationIds.size, 0);
  assert.ok(Math.abs(after.x_mm - before.x_mm) < 0.1, "location X remains physically fixed");
  assert.ok(Math.abs(after.y_mm - before.y_mm) < 0.1, "location Y remains physically fixed");
  for (const key of ["id", "zone_id", "x_mm", "y_mm", "rotation_deg", "width_mm", "depth_mm"]) {
    assert.equal(planning.mappedLocationPallets[0][key], lookup.mappedLocationPallets[0][key], `operational ${key} stays published`);
  }
  assert.deepEqual(Array.from(planning.visualLayout.features, item => item.id), [zone.id, empty.id], "legacy aisle lines stay out of the automatic-passage view");
  const unsaved = render("planning", draft.points.map(([x, y]) => [x + 500, y]));
  assert.ok(Math.abs(unsaved.visualLayout.pallets[0].x_mm - after.x_mm) < 0.1);
  const resized = render("planning", [[10000, 1000], [22000, 1000], [22000, 5000], [10000, 5000]]);
  assert.ok(Math.abs(resized.visualLayout.pallets[0].x_mm - before.x_mm) < 0.1);
  assert.equal(resized.visualLayout.pallets[0].planning_slot_width_mm, 1200, "standard physical footprint does not scale with a zone");
  assert.equal(JSON.stringify(locations), original, "preview does not mutate quantity, identity, version or warehouse records");
  assert.equal(render("lookup").visualLayout.pallets[0].x_mm, before.x_mm, "cancel/mode switch returns to published geometry");
});

test("location draft can retain an out-of-zone move without clamping it back", () => {
  const zone = { points: [[0,0],[6000,0],[6000,4000],[0,4000]] };
  const location = { location_id: 1, map_position: { left_pct: 0, top_pct: 0, width_pct: 20, height_pct: 25, version: 1 } };
  const original = JSON.stringify(location);
  const draft = inventory.locationLayoutGeometry(zone, location, -600, 4500, { clampToZone: false });
  assert.equal(draft.left_pct, -20);
  assert.equal(draft.top_pct, -25);
  assert.equal(draft.width_pct, 20);
  assert.equal(JSON.stringify(location), original);
});

test("location draft save permits conflicts and distinguishes write, apply readback and multi-area failures", async () => {
  for (const failure of ["readback", "write", "multiple", "success"]) {
    const points = [[0,0],[6000,0],[6000,4000],[0,4000]];
    const features = ["TEST", "OTHER"].map(code => ({id:code, erp_area_code:code, feature_kind:"zone", version:1, points}));
    let drafts = { 151: {location_id:151, expected_version:1, left_pct:-10, top_pct:0, height_pct:25, width_pct:20} };
    if (failure === "multiple") drafts[152] = {...drafts[151],location_id:152};
    let writes=0, reads=0; const messages=[]; const noop=()=>{};
    await componentValue("saveLocationDrafts", {
      layout: {source_sha256:"d1",features}, locationEditBusy:false,
      layoutMapToolsOpen:false, locationDrafts:drafts, locationPointEditAreaCode:null, floorCode:"3F",
      dashboard:{locations:[{location_id:151,area_code:"TEST"},{location_id:152,area_code:"OTHER"}]},
      locationProjectionFeatures:features, pointsBoundsMm:()=>({centerXmm:3000,centerYmm:2000,widthMm:6000,heightMm:4000}),
      planningGeometryConflicts:[{pallet_id:"erp-location-151"}],
      setLocationEditBusy:noop, setLocationEditMessage:v=>messages.push(v),
      mutateJson:async(url,method,payload)=>{
        writes++; assert.match(url,/features\/(TEST|OTHER)\/geometry$/);
        assert.equal(payload.ground_locations[0].x_mm,-600);
        if(failure==="write") throw new Error("simulated rejection");
        return {revision:`d${writes+1}`,item:features[writes-1]};
      },
      setLayout:noop,rememberServerDraft:noop,setLocationDrafts:fn=>{drafts=fn(drafts)},
      locationLayoutIdempotencyKey:"test-once",operationKey:()=>"next",setLocationLayoutIdempotencyKey:noop,
      planningPublishedLayout:{source_sha256:"p1"},publishedFloorRevision:"p1",
      setLocationPointEditAreaCode:noop,setKeyboardLocationEditActive:noop,
      refreshPlanningTwinFloor:async()=>{reads++;},
      applySavedAreaGeometryRevision:async()=>{reads++;if(failure==="readback"){const error=new Error("simulated readback unavailable");error.applicationWritten=true;throw error;}}
    })();
    if(failure==="write") {assert.deepEqual(Object.keys(drafts),["151"]);assert.equal(reads,0);assert.match(messages.at(-1),/未确认保存/);}
    else if(failure==="multiple") {assert.deepEqual(Object.keys(drafts).sort(),["151","152"]);assert.equal(writes,0);assert.match(messages.at(-1),/一次只能保存并应用一个区域/);}
    else if(failure==="readback") {assert.equal(Object.keys(drafts).length,0);assert.equal(reads,2);assert.match(messages.at(-1),/已应用 1 个货位调整.*回读失败/);}
    else {assert.equal(Object.keys(drafts).length,0);assert.equal(reads,1);assert.match(messages.at(-1),/已保存并应用 1 个货位调整/);}
  }
});

test("cancelled or rejected publication retains the draft and performs no readback", async () => {
  for (const confirm of [false, true]) {
    let writes = 0;
    const messages = [];
    await componentValue("publishLayoutDraft", {
      layout: { source_sha256: "d2" }, layoutDraftControl: { status: "validated", published_revision: "p1" }, floorCode: "3F",
      setSpatialEditBusy: () => {}, prepareLegacyRackBindingConfirmation: async () => ({ summary: "", request: {} }),
      window: { confirm: () => confirm }, operationKey: () => "test-publish",
      mutateJson: async () => { writes++; throw new Error("simulated version conflict"); },
      refreshPublishedTwinFloor: () => assert.fail("no readback without acknowledgment"),
      setLayoutDraftControl: () => assert.fail("keep unsaved draft on cancellation/rejection"),
      setLocationEditMessage: message => messages.push(message)
    })();
    assert.equal(writes, confirm ? 1 : 0);
    if (confirm) assert.match(messages.at(-1), /^发布布局失败：simulated version conflict$/);
    else assert.deepEqual(messages, []);
  }
});

test("all map commit-and-readback actions keep acknowledged success distinct from read failure", async () => {
  for (const name of ["previewAndPublishLayout", "discardLayoutDraft", "confirmSelectedAreaOnce"]) {
    const messages = [];
    let commits = 0;
    const noop = () => {};
    await componentValue(name, {
      layout: { source_sha256: "d2" }, floorCode: "3F", layoutDraftControl: { has_draft: true, status: "validated", published_revision: "p1" },
      selectedAreaFeature: { id: "zone-test", version: 1 }, simpleAreaCapacity: "1", formalAreaCodeDraft: "TEST", formalAreaNameDraft: "测试区",
      formalAreaOptions: [], selectedExistingAreaId: "", simpleAreaUsage: "semi_finished", simpleAreaLayout: "pallet_ground", planningPublishedRevision: "p1",
      setSpatialEditBusy: noop, setLocationEditMessage: message => messages.push(message), setLayoutDraftControl: noop,
      setPlanningPublishedRevision: noop, operationKey: () => "test-once", window: { confirm: () => true },
      prepareLegacyRackBindingConfirmation: async () => ({ summary: "", request: {} }),
      mutateJson: async path => path.endsWith("validate")
        ? { status: "validated", draft_revision: "d2", warnings: [], blockers: [] }
        : (commits++, { backup_name: "test-backup", published_revision: "p2" }),
      refreshPublishedTwinFloor: async () => { throw new Error("simulated readback unavailable"); },
      refreshPlanningTwinFloor: async () => { throw new Error("simulated readback unavailable"); }, refreshDashboard: async () => {},
    })();
    assert.equal(commits, 1, name);
    assert.match(messages.at(-1), /已发布.*回读失败|草稿已放弃.*回读失败|区域已启用.*回读失败/, name);
  }
});

test("warehouse help is explicitly opened while draft and failure states stay visible", () => {
  assert.match(source, /aria-expanded=\{mapHelpOpen\} aria-controls="warehouse-map-help"/);
  assert.match(source, /mapHelpOpen && <section id="warehouse-map-help"/);
  assert.match(source, /role="status"/);
  assert.match(source, /activeObjectPreview \? "当前对象编辑预览" : "已应用地图"/);
  assert.match(source, /有未保存调整/);
  assert.match(source, /locationEditMessage && <div/);
  assert.doesNotMatch(source, /区域规划：选区域，核对后确认。|排位保存只生成预览/);
});

test("acknowledged publication with failed readback does not report a failed write or allow a stale draft to publish again", async () => {
  const messages = [];
  let publishCalls = 0;
  let control = { status: "validated", published_revision: "p1" };
  let mode = "planning";
  const noop = () => {};
  const context = { layout: { source_sha256: "d2" }, floorCode: "3F", layoutDraftControl: control,
    setSpatialEditBusy: noop, prepareLegacyRackBindingConfirmation: async () => ({ summary: "", request: {} }),
    window: { confirm: () => true }, operationKey: () => "test-publish-once",
    mutateJson: async () => { publishCalls++; return { backup_name: "test-backup" }; },
    refreshPublishedTwinFloor: async () => { throw new Error("simulated readback unavailable"); },
    setMapMode: value => { mode = value; }, setSearchPanelOpen: noop, setLocationEditMode: noop, setAreaPolicyEditMode: noop,
    setRackDrafts: noop, setZonePolicyDrafts: noop, replaceZoneGeometryDrafts: noop,
    setLegacyRackBindingPreview: noop, setLegacyRackBindingSelections: noop,
    setLayoutDraftControl: value => { control = typeof value === "function" ? value(control) : value; },
    setLocationEditMessage: value => messages.push(value) };
  await componentValue("publishLayoutDraft", context)();
  assert.equal(publishCalls, 1);
  assert.equal(mode, "planning", "unverified readback must not display a new lookup layout");
  assert.match(messages.at(-1), /已发布.*回读失败/);
  assert.doesNotMatch(messages.at(-1), /^发布布局失败/);
  assert.notEqual(control?.status, "validated", "old validated revision cannot be submitted again");
});

test("ordinary area planning exposes a current-area-only point editing workflow", () => {
  assert.doesNotMatch(source, />拖动并保存现场货位</);
  assert.match(source, />保存货位调整/);
  assert.match(source, />取消点位调整/);
  assert.match(source, /location\?\.area_code !== locationPointEditAreaCode/);
  assert.match(source, /draggablePalletIds=\{warehouseMoveModeActive \? movablePalletIds : layoutMapToolsOpen \? EMPTY_CANVAS_IDS : locationPointEditPalletIds\}/);
  assert.match(source, /mergePublishedFeatureGeometry/);
});

test("point save is the single explicit action and keeps inventory outside the write scope", () => {
  assert.match(source, /红色冲突可先保存；系统会尝试应用/);
  assert.match(source, /库存数量与栈板绑定不变/);
  assert.match(source, /保存通过校验后，查货、移货和盘点立即使用同一位置/);
  assert.doesNotMatch(source, /确认保存并固定/);
  assert.doesNotMatch(source, /occupiedDraftCount/);
});

test("area planning gives a clicked location priority over its enclosing area", () => {
  assert.match(editorSource, /const preferredPlanningPallet = palletEditingOnly/);
  assert.match(editorSource, /candidate\.userData\.entityKind === "pallet" && candidate\.userData\.draggable/);
  assert.match(editorSource, /preferredPlanningPallet \|\| preferredPlanningFeature \|\| roots\[0\]/);
  assert.match(source, /if \(!locationEditMode \|\| layoutMapToolsOpen\) return/);
  assert.match(source, /setLocationPointEditAreaCode\(location\.area_code\)/);
  assert.match(source, /palletEditingOnly=\{locationEditMode \|\| warehouseMoveModeActive\}/);
});

test("area planning shows every ground location with a full colored slot footprint", () => {
  assert.match(source, /layout\?\.id,\s*locationEditMode/);
  assert.match(source, /区域规划按已应用地图显示全部正式货位/);
  assert.match(source, /奶白色为空货位，绿色为有货货位/);
  assert.match(inventorySource, /const isPlanningLocationSlot = isGroundLocation/);
  assert.match(inventorySource, /planning_slot_width_mm: isPlanningLocationSlot \? standard\.width_mm/);
  assert.match(inventorySource, /planning_slot_depth_mm: isPlanningLocationSlot \? standard\.depth_mm/);
  assert.match(sceneSource, /pallet\.is_planning_location_slot/);
  assert.match(sceneSource, /new THREE\.EdgesGeometry\(geometry\)/);
});

test("operational column conflicts stay separate from planning geometry hints", () => {
  assert.match(source, /const operationalColumnConflicts = useMemo\([\s\S]*findPalletColumnConflicts\(/);
  assert.match(source, /const planningGeometryConflicts = useMemo\([\s\S]*layout \? findPalletPlanningConflicts\(/);
  assert.match(source, /const displayedLocationConflicts = planningGeometryConflicts/);
  assert.match(source, /!operationalColumnConflictIds\.has\(`erp-location-\$\{location\.location_id\}`\)/);
  assert.match(source, /column_conflicts: operationalColumnConflictCount/);
  assert.match(source, /uniquePalletConflictCount\(operationalColumnConflicts\)/);
  assert.match(source, /findPalletPlanningConflicts\([\s\S]*prospectivePallets/);
  assert.doesNotMatch(componentValue("saveLocationDrafts", {}).toString(), /conflictingAreaLocationIds/);
});

test("planning location saves use versioned map drafts instead of operational position writes", () => {
  const body = componentValue("saveLocationDrafts", {}).toString();
  assert.match(body, /ground_locations/);
  assert.match(body, /expected_revision: workingLayout.source_sha256/);
  assert.match(body, /expected_version: feature.version/);
  assert.doesNotMatch(body, /published-positions/);
});

test("map-first toolbar hides empty delayed dispatch and consolidates selective merge", () => {
  assert.match(source, /const \[delayedDispatchOpen, setDelayedDispatchOpen\] = useState\(false\)/);
  assert.match(source, /!!dashboard\?\.delayed_dispatch_relocation\?\.candidate_count/);
  assert.match(source, /延期待送 \{dashboard\.delayed_dispatch_relocation\.candidate_count\}/);
  assert.match(source, /delayedDispatchOpen && dashboard\?\.delayed_dispatch_relocation/);
  assert.doesNotMatch(source, />自动合并</);
  assert.match(source, /onClick=\{openAutomaticMerge\}>合并栈板</);
  assert.match(source, />可合并货位</);
  assert.doesNotMatch(source, /只勾选现场要合并的栈板/);
  assert.match(source, /type="checkbox" checked=\{selected\}/);
});

test("warehouse header keeps label printing in the low-frequency ledger", () => {
  assert.doesNotMatch(source, /label_print=1/);
  assert.doesNotMatch(source, />打印货位编号<\/a>/);
  assert.match(source, /href="\/warehouse-ledger\.html\?tab=finished"/);
});

test("warehouse header keeps the ledger link on the command row", () => {
  assert.match(cssSource, /grid-template-columns:\s*minmax\(680px, 1fr\) auto auto/);
  assert.match(cssSource, /@media \(max-width: 1180px\)[\s\S]*grid-template-columns:\s*minmax\(470px, 1fr\) auto auto/);
});

test("lookup has one entry and area planning uses short adaptive actions", () => {
  const toolbar = source.slice(source.indexOf('<section className="twin-toolbar">'), source.indexOf('<section className={`twin-workspace'));
  assert.doesNotMatch(toolbar, /twin-warehouse-search-toggle/);
  assert.match(source, /<header><h2>查货<\/h2><\/header>/);
  assert.match(source, /<b>用途与容量<\/b>/);
  assert.match(source, /: "确认"\}<\/button>/);
  assert.match(source, />编辑<\/button>/);
  assert.match(source, />货位\/货架<\/button>/);
  assert.match(source, />发布<\/button>/);
  assert.doesNotMatch(source, />确认并启用此区域<\/button>/);
});

test("area planning keeps short inputs in compact rows", () => {
  assert.match(source, /className="twin-zone-primary-fields"/);
  assert.match(source, /className="twin-zone-confirm-row"/);
  assert.match(source, /className="twin-zone-policy-fields"/);
  assert.match(source, /className="twin-rack-primary-fields"/);
  assert.match(source, /className="twin-mold-rack-fields"/);
  assert.match(cssSource, /\.twin-zone-primary-fields,[\s\S]*\.twin-mold-rack-fields\s*\{[\s\S]*repeat\(auto-fit, minmax\(100px, 1fr\)\)/);
  assert.match(cssSource, /\.twin-zone-geometry-grid\s*\{[\s\S]*repeat\(4, minmax\(0, 1fr\)\)/);
  assert.doesNotMatch(source, /地堆排、位编号|编号起点|起始排号|起始位号/);
});

test("planning dimensions save the latest input and adjustment locks map panning", () => {
  assert.match(source, /const \[layoutMapToolsOpen, setLayoutMapToolsOpen\] = useState\(false\)/);
  assert.match(source, /const zoneGeometryDraftsRef = useRef<Record<string, number\[\]\[\]>>\(\{\}\)/);
  assert.match(source, /zoneGeometryDraftsRef\.current\[selectedAreaFeature\.id\]/);
  assert.match(source, /mapPanLocked=\{floor4CalibrationMode \|\| \(locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust"\)\}/);
  assert.match(editorSource, /mapPanLocked\?: boolean/);
  assert.match(editorSource, /controls\.enablePan = !mapPanLocked/);
});

test("aligned floor 4 keeps one compact three-point recalibration action", () => {
  const beginCalibration = source.slice(
    source.indexOf("const beginFloor4Calibration"),
    source.indexOf("const cancelFloor4Calibration")
  );
  const calibrationAction = source.slice(
    source.indexOf('{canEditLocations && floorCode === "4F"'),
    source.indexOf("{canEditLocations && staleLayoutDraft")
  );
  assert.doesNotMatch(beginCalibration, /floor4CalibrationApplied\) return/);
  assert.doesNotMatch(calibrationAction, /disabled=\{[^}]*floor4CalibrationApplied/);
  assert.match(calibrationAction, /重新标定货梯\/朝向/);
  assert.match(beginCalibration, /setLayers\(\(current\) => \(\{ \.\.\.current, structures: true \}\)\)/);
  assert.match(beginCalibration, /replaceZoneGeometryDrafts\(\{\}\)/);
  assert.match(source, /货梯标定 0\/3：请点击货梯门口第一端/);
  assert.match(source, /已记录门口第一端；请点击门口另一端/);
  assert.match(source, /已记录门口宽度；请点击货梯内侧后沿/);
  assert.match(source, /calibration_mode: "doorway_heading"/);
  assert.match(source, /mapPanLocked=\{[^}]*floor4CalibrationMode/);
  assert.match(editorSource, /calibrationMode[\s\S]*layout\.floor_code\.toUpperCase\(\) === "4F"[\s\S]*feature\.feature_code === "LIFT-002"/);
  assert.match(source, /四楼实测成品仓库（重新校正中）/);
  assert.match(source, /按门口两端和内侧后沿点选 · 地图已锁定/);
  assert.match(editorSource, /floor4CalibratingCompass \? "对齐3F"/);
  assert.match(source, /已取消货梯标定，4F 草稿没有改变/);
});

test("planning exits to lookup and map geometry tools open only on demand", () => {
  assert.match(source, /setMapMode\("lookup"\);[\s\S]*setSearchPanelOpen\(true\)/);
  assert.match(source, /\{layoutMapToolsOpen \? "完成地图调整" : "调整地图"\}<\/button>/);
  assert.match(source, /setLayoutMapToolsOpen\(\(current\) => !current\)/);
  assert.match(source, /mapMode === "planning" && locationEditMode && canEditLocations && layoutMapToolsOpen && <section className="twin-layout-map-tools">/);
  assert.match(source, /featureEditingEnabled=\{!spatialEditBusy && locationEditMode && layoutMapToolsOpen && !locationPointEditAreaCode && layoutMapTool === "adjust"\}/);
  assert.match(source, /layoutDrawKind = locationEditMode && layoutMapToolsOpen && layoutMapTool !== "adjust"/);
});

test("planning moves boundaries without moving formal locations", () => {
  assert.match(editorSource, /planningFeatureEditable[\s\S]*\["zone", "aisle"\]\.includes\(feature\.feature_kind\)/);
  assert.doesNotMatch(
    editorSource.slice(editorSource.indexOf("const planningFeatureEditable"), editorSource.indexOf("group.userData =", editorSource.indexOf("const planningFeatureEditable"))),
    /feature\.is_locked/
  );
  assert.match(source, /mapPanLocked=\{floor4CalibrationMode \|\| \(locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust"\)\}/);
  assert.match(source, /rackEditingEnabled=\{locationEditMode && Boolean\(selectedRackEditDraft\) && !spatialEditBusy\}/);
  assert.match(editorSource, /preferredPlanningFeature[\s\S]*candidate\.userData\.entityKind === "feature" && candidate\.userData\.draggable/);
  assert.match(source, /featureHasMappedGroundLocations/);
  assert.match(source, /区域移动不带动货位/);
  assert.doesNotMatch(componentValue("moveAreaBoundaryDraft", {}).toString(), /featureHasMappedGroundLocations/);
});

test("map adjustment and location placement are mutually exclusive", () => {
  assert.match(source, /const locationProjectionFeatures = useMemo/);
  assert.match(source, /\(\) => mergePublishedFeatureGeometry\(features, planningPublishedFeatures\)/);
  assert.match(source, /const planningVisibleFeatures = useMemo/);
  assert.match(source, /findPalletPlanningConflicts\([\s\S]*planningVisibleFeatures/);
  assert.match(source, /features: planningVisibleFeatures\.map/);
  assert.doesNotMatch(source, /locationEditMode && !layoutMapToolsOpen \? locationProjectionFeatures : layout\.features/);
  assert.match(source, /setLayoutMapToolsOpen\(false\)/);
  assert.match(source, /layoutMapToolsOpen \? EMPTY_CANVAS_IDS : locationPointEditPalletIds/);
  assert.match(source, /!locationPointEditAreaCode && layoutMapTool === "adjust"/);
  assert.match(source, /activeLocationDraftCount > 0/);
  assert.match(source, /if \(!layout \|\| layoutMapToolsOpen \|\| locationEditBusy\)/);
});

test("warehouse racks use exact footprint picking and only horizontal or vertical direction", () => {
  assert.match(editorSource, /function warehouseRackPickProxy\(rack: Rack\)/);
  assert.match(editorSource, /new THREE\.BoxGeometry\(width, pickHeight, depth\)/);
  assert.match(editorSource, /warehouseRackPickProxy\(rack\)/);
  assert.match(sceneSource, /if \(!warehouseTheme\) \{[\s\S]*accessDirectionVectors\(rack\.access_side\)/);
  const direction = source.slice(source.indexOf('aria-label="货架方向"'), source.indexOf('</select>', source.indexOf('aria-label="货架方向"')));
  assert.match(direction, /横向 0°/);
  assert.match(direction, /竖向 90°/);
  assert.doesNotMatch(direction, /180°|270°/);
  assert.doesNotMatch(source, /<span>正面方向<\/span>/);
});

test("rack focus keeps the map visible beside an ERP styled elevation", () => {
  assert.match(source, /focusedRack \? "rack-focused" : ""/);
  assert.match(source, /className="twin-map-pane"/);
  assert.match(source, /className="twin-rack-map-callout"/);
  assert.match(source, /className="twin-rack-focus-panel twin-rack-stage/);
  assert.doesNotMatch(source, /className="twin-rack-modal"/);
  assert.match(cssSource, /\.twin-stage\.rack-focused\s*\{[\s\S]*grid-template-columns:\s*minmax\(260px, 1fr\) minmax\(0, 2fr\)/);
  assert.match(cssSource, /@media \(max-width: 880px\)[\s\S]*\.twin-stage\.rack-focused\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
});

test("saving geometry preserves the area's warehouse binding used by quantity controls", async () => {
  const original = { id: "zone-d1", feature_kind: "zone", version: 3, formal_area_id: 9, formal_policy_status: "published", points: [[0, 0]] };
  let layout = { source_sha256: "before", features: [original] };
  let drafts = { "zone-d1": [[1, 0]] };
  const context = { layout, spatialEditBusy: false, floorCode: "3F", operationKey: () => "test",
    planningPublishedLayout: { source_sha256: "published" }, publishedFloorRevision: "published",
    setSpatialEditBusy: () => {}, setLocationEditMessage: () => {}, rememberServerDraft: () => {},
    mutateJson: async () => ({ revision: "after", item: { id: "zone-d1", feature_kind: "zone", version: 4, points: [[1, 0]] } }),
    applySavedAreaGeometryRevision: async () => ({ applied: true }),
    setLayout: (update) => { layout = update(layout); }, replaceZoneGeometryDrafts: (update) => { drafts = update(drafts); } };
  await componentValue("saveLayoutFeatureGeometry", context)(original, [[1, 0]]);
  assert.equal(layout.features[0].formal_area_id, 9);
  assert.equal(layout.features[0].formal_policy_status, "published");
  assert.equal(layout.features[0].version, 4);
  assert.equal(layout.features[0].points[0][0], 1);
  assert.equal(Object.keys(drafts).length, 0);
});

test("warehouse floor switch exposes only operational floors", () => {
  assert.match(source, /switchWarehouseFloor\("1F"\)/);
  assert.match(source, /switchWarehouseFloor\("3F"\)/);
  assert.match(source, /switchWarehouseFloor\("4F"\)/);
  assert.doesNotMatch(source, />2F<\/b>/);
  assert.doesNotMatch(source, />5F<\/b>/);
});

test("rack elevation warns only for occupied unbound legacy locations", () => {
  assert.match(source, /location\.occupancy_status === "occupied" \|\| rackLocationInventoryItems\(location\)\.length > 0/);
  assert.match(source, /个有货旧货位未绑定货架层格，请先转入盘点待归位/);
  assert.doesNotMatch(source, /个旧货位未绑定货架层格，未纳入本货架正视图/);
});

test("planning derives passages from floor space outside zones and keeps legacy aisles hidden", () => {
  assert.match(source, /deleteSelectedLayoutFeature/);
  assert.match(source, /deleteSelectedLayoutFeature = \(\) => deleteLayoutFeature\(selectedLayoutFeature\)/);
  assert.match(source, /\/features\/\$\{feature\.id\}\?\$\{query\.toString\(\)\}/);
  assert.match(source, /className="twin-feature-context-menu"/);
  assert.match(source, /onFeatureContextMenu=\{locationEditMode/);
  assert.match(source, /selectedLayoutFeature\?\.feature_kind === "zone"/);
  assert.match(source, /aisleEditingEnabled=\{false\}/);
  assert.match(source, /filter\(\(feature\) => feature\.feature_kind !== "aisle"\)/);
  assert.match(source, /区域外自动作为通道/);
  assert.doesNotMatch(source, />新增区域<\/button>/);
  assert.doesNotMatch(source, />新增通道<\/button>/);
  assert.doesNotMatch(source, /<option value="aisle">新增通道<\/option>/);
  assert.doesNotMatch(source, /selectedAisle/);
});

test("ordinary planning no longer offers tight automatic pallet packing", () => {
  assert.doesNotMatch(source, />自动均匀排布空闲系统货位</);
  assert.match(source, /系统不强制紧贴均匀排布/);
});

test("count changes send the complete location snapshot even when it is empty", () => {
  assert.match(source, /target_count: targetCount,[\s\S]*expected_layout_versions: selectedAreaLayoutVersions/);
  assert.doesNotMatch(source, /expected_layout_versions: selectedAreaLocationCount \?/);
});

test("logical map positions keep a small renderer anchor", () => {
  assert.match(sceneSource, /pallet\.is_logical_anchor \? 180 : 400/);
});

test("unexpected unlocated blocker stays complete while deliberate recount uses its own list", () => {
  assert.match(source, /unlocatedFinishedItems\.map\(\(item\)/);
  assert.doesNotMatch(source, /unlocatedFinishedItems\.slice\(/);
  assert.match(source, /unlocated_inventory \|\| \[\]\)\.filter\(\(item\) => !item.pending_relocation && inventoryHasPhysicalQuantity\(item\)\)/);
  assert.match(source, /unlocated_inventory \|\| \[\]\)\.filter\(\(item\) => item.pending_relocation && inventoryHasPhysicalQuantity\(item\)\)/);
  assert.match(source, /inventoryPhysicalQuantity\(item\)/);
});

test("dashboard locations normalize stale empty pallet wrappers before rendering and moving", () => {
  assert.match(source, /normalizeInventoryLocationProjection\(location\)/);
  assert.match(source, /currentFloorOccupiedLocations/);
  assert.doesNotMatch(source, /currentFloor\?\.occupied_locations \|\| 0/);
});

import assert from "node:assert/strict";
import test from "node:test";

import {
  floor1CandidateBlockerDetail,
  floor1CandidateBlockerHref
} from "../src/floor1CandidateBlockers.mjs";

test("DISPATCH inventory blocker opens the exact formal location in move mode", () => {
  const href = floor1CandidateBlockerHref({
    action_kind: "open_inventory_move",
    area_code: "DISPATCH",
    location_id: 401,
    live_lot_count: 17,
    current_pallet_count: 5
  });
  const url = new URL(href, "http://erp.local");
  assert.equal(url.pathname, "/warehouse.html");
  assert.equal(url.searchParams.get("floor"), "1F");
  assert.equal(url.searchParams.get("view"), "2d");
  assert.equal(url.searchParams.get("mode"), "move");
  assert.equal(url.searchParams.get("location_id"), "401");
  assert.equal(url.searchParams.get("area_code"), "DISPATCH");
  assert.equal(url.searchParams.has("edit"), false);
  assert.equal(
    floor1CandidateBlockerDetail({
      action_kind: "open_inventory_move",
      live_lot_count: 17,
      current_pallet_count: 5
    }),
    "待处理 17 个库存批次 · 5 个实体栈板"
  );
});

test("policy blocker opens the exact map feature in area planning", () => {
  const href = floor1CandidateBlockerHref({
    action_kind: "open_area_planning",
    area_code: "OLD-A",
    map_feature_id: "ZONE-1F-OLD-A"
  });
  const url = new URL(href, "http://erp.local");
  assert.equal(url.searchParams.get("mode"), "planning");
  assert.equal(url.searchParams.get("area_code"), "OLD-A");
  assert.equal(url.searchParams.get("map_feature_id"), "ZONE-1F-OLD-A");
  assert.equal(url.searchParams.get("edit"), "area_policy");
});

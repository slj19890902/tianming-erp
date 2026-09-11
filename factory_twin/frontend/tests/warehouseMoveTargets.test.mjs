import test from "node:test";
import assert from "node:assert/strict";
import {intersectMappedMoveTargets, upsertMoveDraft} from "../src/warehouseMoveDraft.mjs";

test("single products include occupied targets, whole pallets still require empty", () => {
  const candidates = [{id: 1, is_empty: true}, {id: 2, is_empty: false, occupied: true}];
  const locations = candidates.map(c => ({location_id:c.id, is_active:true, floor_code:"3F",
    occupancy_status:c.is_empty ? "empty" : "occupied", position_status:"mapped", map_position:{version:1}}));
  assert.deepEqual(intersectMappedMoveTargets(candidates, locations, [], [], "lot_transfer").map(l => l.location_id), [1,2]);
  assert.deepEqual(intersectMappedMoveTargets(candidates, locations, [], [], "pallet_move").map(l => l.location_id), [1]);
  assert.deepEqual(intersectMappedMoveTargets(candidates, locations, [2], [1], "lot_transfer"), []);
});

test("multiple product drafts may share a target, but cannot share with a whole pallet", () => {
  const first={source_key:"lot:1", operation:"lot_transfer", target_location_id:3, client_item_id:"1"};
  const next={...first,source_key:"lot:2",client_item_id:"2"};
  assert.equal(upsertMoveDraft([first], next).items.length,2);
  assert.equal(upsertMoveDraft([first], next).error,null);
  assert.ok(upsertMoveDraft([first], {...next,operation:"pallet_move"}).error);
  assert.ok(upsertMoveDraft([{...first,operation:"pallet_move"}], next).error);
});

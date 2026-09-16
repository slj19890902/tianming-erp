import test from "node:test";
import assert from "node:assert/strict";
import {stocktakeAddBlockReason, upsertStocktakeDraft, buildStocktakeBatchPayload} from "../src/warehouseStocktakeDraft.mjs";
test("normal goods locations accept three types while operation and unpublished locations stay blocked", () => {
 const base={location_id:1,is_active:true,position_status:"mapped",map_position:{version:1},floor_code:"1F",source_version:"TWIN_V1",area_code:"FG",location_code:"FG-1",storage_type:"ground"};
 for(const warehouse_type of ["finished","semi_finished","shared"]) for(const kind of ["finished","semi_finished","raw_material"]){
  assert.equal(stocktakeAddBlockReason({...base,warehouse_type},kind),null);
  assert.ok(stocktakeAddBlockReason({...base,warehouse_type,area_code:"DISPATCH"},kind));
  assert.ok(stocktakeAddBlockReason({...base,warehouse_type,is_active:false},kind));
  assert.ok(stocktakeAddBlockReason({...base,warehouse_type,position_status:"unplaced"},kind));
 }
});
test("mixed draft identities and units stay independent",()=>{
 let drafts=[];
 for(const kind of ["finished","semi_finished","raw_material"]){
  const result=upsertStocktakeDraft(drafts,{client_item_id:kind,operation:"add",location_id:1,expected_layout_version:1,customer_id:2,product_id:3,inventory_type:kind,unit:kind==="finished"?"boxes":"sheets",quantity:5,stock_date:"2026-09-10"});
  assert.equal(result.error,null);drafts=result.items;
 }
 assert.equal(drafts.length,3);
 assert.deepEqual(buildStocktakeBatchPayload("mixed",drafts).items.map(i=>i.inventory_type),["finished","semi_finished","raw_material"]);
});
test("body and complete stock drafts never merge and preserve their stage",()=>{
 const base={operation:"add",location_id:1,expected_layout_version:1,customer_id:2,product_id:3,inventory_type:"finished",unit:"boxes",quantity:5,stock_date:"2026-09-16"};
 let drafts=[];
 for(const stage of ["complete","body"]){
  const result=upsertStocktakeDraft(drafts,{...base,client_item_id:stage,stock_stage:stage});
  assert.equal(result.error,null);drafts=result.items;
 }
 assert.equal(drafts.length,2);
 assert.deepEqual(buildStocktakeBatchPayload("stages",drafts).items.map(i=>i.stock_stage || "complete"),["complete","body"]);
});

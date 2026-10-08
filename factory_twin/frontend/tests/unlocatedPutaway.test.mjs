import test from 'node:test';
import assert from 'node:assert/strict';
import {unlocatedPalletChoice} from '../src/unlocatedPutaway.mjs';
const item={lot_id:72,location_id:9,available_quantity:0,reserved_quantity:180};
const pallet={pallet_id:3,version:5,move_eligible:true,items:[{lot_id:72}]};
const location={location_id:9,position_status:'area_only',map_position:null,pallets:[{...pallet,pallet_id:2,items:[{lot_id:71}]},pallet]};
test('unmapped reserved stock chooses its own pallet, preserving original versions and no writes',()=>{
 const before=JSON.stringify(location);const choice=unlocatedPalletChoice(item,[location]);
 assert.equal(choice.pallet,pallet);assert.equal(choice.location,location);assert.equal(JSON.stringify(location),before);
});
test('missing and ambiguous ownership cannot guess another pallet',()=>{
 assert.match(unlocatedPalletChoice(item,[]).error,/位置/);
 assert.match(unlocatedPalletChoice({...item,lot_id:99},[location]).error,/唯一/);
 assert.match(unlocatedPalletChoice(item,[{...location,pallets:[pallet,{...pallet,pallet_id:4}]}]).error,/唯一/);
});
test('backend eligibility and current pallet version are required; legacy single projection works',()=>{
 assert.equal(unlocatedPalletChoice(item,[{...location,pallets:[{...pallet,move_eligible:false,move_block_reason:'质量冻结'}]}]).error,'质量冻结');
 assert.ok(unlocatedPalletChoice(item,[{...location,pallets:[{...pallet,version:0}]}]).error);
 assert.ok(unlocatedPalletChoice(item,[{...location,pallets:[{...pallet,move_eligible:undefined}]}]).error);
 assert.equal(unlocatedPalletChoice(item,[{...location,pallets:[],pallet}]).pallet,pallet);
});

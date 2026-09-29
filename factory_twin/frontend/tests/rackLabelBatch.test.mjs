import test from 'node:test';
import assert from 'node:assert/strict';
import {areaRackLabelBatch} from '../src/rackLabelBatch.mjs';
const racks=[{id:'b',name:'R01',x_mm:20,y_mm:0},{id:'a',name:'R01',x_mm:0,y_mm:0}];
const locations=[{location_id:1,map_rack_id:'a',floor_code:'1F',is_active:true,level_no:1,slot_no:1},{location_id:2,map_rack_id:'a',floor_code:'1F',is_active:true,level_no:2,slot_no:1},{location_id:3,map_rack_id:'b',floor_code:'1F',is_active:true}];
test('one per stable rack, same names distinct, spatial order and direct rack mode',()=>{assert.deepEqual(areaRackLabelBatch(racks,locations,'1F'),{count:2,error:'',url:'/static/shelf-label.html?content=rack&location_ids=1,3'});});
test('reject missing, wrong floor and inactive locations without silently omitting racks',()=>{for(const input of [locations.slice(0,2),locations.map(x=>({...x,floor_code:'2F'})),locations.map(x=>({...x,is_active:false}))]){const r=areaRackLabelBatch(racks,input,'1F');assert(r.error);assert.equal(r.url,'');}});
test('empty or oversized selections cannot print',()=>{assert(areaRackLabelBatch([],[],'1F').error);const many=Array.from({length:501},(_,i)=>({id:String(i),x_mm:i,y_mm:0}));assert(areaRackLabelBatch(many,many.map((r,i)=>({location_id:i+1,map_rack_id:r.id,floor_code:'1F',is_active:true})),'1F').error);});

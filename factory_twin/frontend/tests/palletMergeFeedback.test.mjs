import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import {randomUUID} from 'node:crypto';
import {buildPalletMergeBatchPayload} from '../src/warehousePalletMergeDraft.mjs';
const source=fs.readFileSync(new URL('../src/WarehouseTwinApp.tsx',import.meta.url),'utf8');
const canvas=fs.readFileSync(new URL('../src/EditorCanvas.tsx',import.meta.url),'utf8');
const compile=s=>ts.transpileModule(s,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
test('HTTPS merge key fits API 64 limit without removing UUID entropy; fallback fits too',()=>{
  const code=compile(source.slice(source.indexOf('function operationKey('),source.indexOf('function rackClearHeights(')));
  for(const crypto of [{randomUUID},{}]){
    const ctx={crypto};vm.createContext(ctx);vm.runInContext(code,ctx);
    const key=ctx.operationKey('warehouse-pallet-merge-batch');
    assert.ok(key.length<=64);assert.notEqual(key,ctx.operationKey('warehouse-pallet-merge-batch'));
    const target={pallet_id:2,expected_version:3};
    const payload=buildPalletMergeBatchPayload(key,[{pallet_id:1,expected_version:4,client_item_id:'source'},target],target);
    assert.equal(payload.idempotency_key,key);assert.equal(payload.sources.length,1);
    assert.equal(payload.expected_target_version,3);
  }
});
test('failed merge keeps same retry key and draft; successful merge clears once',async()=>{
  const code=compile(source.slice(source.indexOf('  const confirmPalletMergeBatch ='),source.indexOf('  const confirmMoveDrafts =')));
  for(const fail of [true,false]){
    const calls=[],clears=[],messages=[];
    const target={pallet_id:2,expected_version:1,location_id:20,floor_code:'3F',location_name:'主货位'};
    const ctx={mergeSources:[{pallet_id:1,expected_version:1,client_item_id:'src'},target],mergeTarget:target,mergeBatchBusy:false,mergeBatchIdempotencyKey:'stable-key',window:{confirm:()=>true},buildPalletMergeBatchPayload,
      mutateJson:async(p,m,b)=>{calls.push(b);if(fail)throw new Error('校验失败');},
      setMergeBatchBusy:()=>{},setWarehouseOperationMessage:m=>messages.push(m),setMergeSources:v=>clears.push(v),setMergeTarget:()=>{},setMergeBatchIdempotencyKey:v=>clears.push(v),operationKey:()=> 'new-key',isWarehouseOperationalFloorCode:()=>true,setFloorCode:()=>{},setSelected:()=>{},refreshDashboard:async()=>{}};
    vm.createContext(ctx);vm.runInContext(code+'\nglobalThis.submit=confirmPalletMergeBatch;',ctx);await ctx.submit();
    assert.equal(calls.length,1);assert.equal(calls[0].idempotency_key,'stable-key');
    assert.equal(clears.length,fail?0:2);assert.ok(messages.at(-1).includes(fail?'失败':'已一次并入'));
  }
});
test('merge target is a separate amber map highlight and feedback appears in draft footer',()=>{
  const fn=canvas.slice(canvas.indexOf('function syncResultHighlights('),canvas.indexOf('function animateFocus('));
  const hits=[],ctx={clearHighlightGroup:()=>{},addEntityHighlight:(g,o,c)=>hits.push(c)};
  vm.createContext(ctx);vm.runInContext(compile(fn),ctx);
  const runtime={resultHighlight:{},entityNodes:new Map([['pallet:erp-location-20',{}]]),requestRender:()=>{}};
  ctx.syncResultHighlights(runtime,[],[],'erp-location-20');assert.deepEqual(hits,[0xf59e0b]);
  hits.length=0;ctx.syncResultHighlights(runtime,[],[],undefined);assert.equal(hits.length,0);
  assert.ok(source.includes('mergeTargetPalletId={mapMode === "move" && moveAction === "merge"'));
  const footer=source.slice(source.indexOf('<div className="twin-move-draft-heading"><b>{mergeSources.length'));
  assert.ok(footer.indexOf('role="status"')<footer.indexOf('onClick={confirmPalletMergeBatch}'));
});

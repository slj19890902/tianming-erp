import assert from 'node:assert/strict';
import test from 'node:test';
import {createMoldRackPrinter} from '../src/moldRackPrint.mjs';
test('rack/cell entry opens the real selection page directly without requests or print registration',async()=>{
 const urls=[],states=[];
 const printer=createMoldRackPrinter({openWindow:url=>{urls.push(url);return{}},changed:s=>states.push(s)});
 await printer.run({floorCode:'1F',rackId:'rack A',cellId:'stable-a3'},'mold_80x40_v1');
 const url=new URL(urls[0],'http://erp');assert.equal(url.pathname,'/static/mold-print-select.html');
 assert.equal(url.searchParams.get('rack_id'),'rack A');assert.equal(url.searchParams.get('cell_id'),'stable-a3');
 assert.equal(url.searchParams.get('template_version'),'mold_80x40_v1');assert.equal(states.at(-1).pending,false);
 await printer.run({floorCode:'3F',rackId:'legacy',level:2,grid:3});
 assert.match(urls[1],/level=2&grid=3/);
});
test('blocked entry is visible and does not close windows or create a pending print',async()=>{
 const states=[];const p=createMoldRackPrinter({openWindow:()=>null,changed:s=>states.push(s)});
 await p.run({floorCode:'1F',rackId:'a'});assert.match(states.at(-1).message,/弹窗/);assert.equal(p.retry(),undefined);
});

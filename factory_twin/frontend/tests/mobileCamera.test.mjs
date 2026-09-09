import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../../../static/shelf-camera.js',import.meta.url),'utf8');
function harness(media=async()=>({getTracks:()=>[]})) {
  const elements=new Map(); let loads=0;
  const $=key=>{if(!elements.has(key)) elements.set(key,{hidden:false,disabled:false,textContent:'',play:async()=>{}});return elements.get(key);};
  const ctx={URL,Set,Promise,Error,setTimeout,clearTimeout,$,location:{origin:'https://tianmingerp0909.share.zrok.io',hostname:'tianmingerp0909.share.zrok.io'},window:{isSecureContext:true,jsQR:()=>null},navigator:{mediaDevices:{getUserMedia:media}},document:{},load:async()=>{loads++;},id:null,product:null};
  vm.createContext(ctx);vm.runInContext(source.slice(0,source.indexOf("\n$('cameraStart').onclick")),ctx);
  return {ctx,$,loads:()=>loads};
}
test('only known ERP shelf URLs become same-origin identities, never arbitrary navigation',()=>{
  const {ctx}=harness();
  for(const value of ['https://evil.example/q/12','javascript:alert(1)','https://tianmingerp0909.share.zrok.io.evil/q/12','/q/0','/q/12/bad','/P/12'])assert.equal(ctx.shelfIdentity(value),null);
  assert.equal(ctx.shelfIdentity('http://192.168.3.80:8000/q/1884').id,'1884');
  assert.equal(ctx.shelfIdentity('/q/1884/'+'a'.repeat(24)).product,'a'.repeat(24));
  assert.equal(ctx.shelfIdentity('/warehouse.html?location_id=1884').id,'1884');
});
test('repeat scans reuse inventory loader without navigation; invalid scan makes no request',async()=>{
  const {ctx,loads}=harness();
  assert.equal(await ctx.acceptShelf('/q/12'),true); assert.equal(ctx.id,'12');
  await ctx.acceptShelf('/q/13'); assert.equal(ctx.id,'13'); assert.equal(loads(),2);
  assert.equal(await ctx.acceptShelf('https://evil.example/q/14'),false);assert.equal(loads(),2);
});
test('late camera permission after cancellation releases every track',async()=>{
  let resolve,stops=0; const pending=new Promise(r=>resolve=r);
  const {ctx}=harness(()=>pending);
  const start=ctx.startCamera();ctx.stopCamera();
  resolve({getTracks:()=>[{stop:()=>stops++}]});await start;assert.equal(stops,1);
});
test('permission rejection gives actionable message and re-enables start',async()=>{
  const {ctx,$}=harness(async()=>{const e=Error('denied');e.name='NotAllowedError';throw e;});
  await ctx.startCamera();assert.match($('cameraStatus').textContent,/Safari/);assert.equal($('cameraStart').disabled,false);
});
test('plain HTTP never requests camera and offers HTTPS entry',async()=>{
  let calls=0;const {ctx,$}=harness(async()=>{calls++;});ctx.window.isSecureContext=false;
  await ctx.startCamera();assert.equal(calls,0);assert.equal($('cameraHttps').hidden,false);
});

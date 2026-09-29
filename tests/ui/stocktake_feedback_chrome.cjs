const fs=require('fs'),http=require('http'),path=require('path'),assert=require('assert/strict');
const root=process.cwd(),src=root+'/factory_twin/frontend/src';
const esbuild=require(root+'/factory_twin/frontend/node_modules/esbuild');
const {chromium}=require('playwright');
const original=fs.readFileSync(src+'/WarehouseTwinApp.tsx','utf8');
const start=original.indexOf('  const confirmStocktakeDrafts =');
const handler=original.slice(start,original.indexOf('\n  const updateRackDraft',start));
const bstart=original.indexOf('        {warehouseOperationMessage &&',original.indexOf('aria-label={moveAction === "stocktake" ? "盘点调整页面草稿汇总"'));
const body=original.slice(bstart,original.indexOf('      </> : moveAction === "merge"',bstart));
assert(bstart>0);assert(body.includes('role="alert"'));
const app=`import React,{useState} from 'react';import {createRoot} from 'react-dom/client';
import {apiErrorMessage} from './warehouseApiError.mjs';import {buildStocktakeBatchPayload,clearStocktakeDrafts,stocktakeBlockResolution} from './warehouseStocktakeDraft.mjs';
function App(){
const [stocktakeDrafts,setStocktakeDrafts]=useState([{client_item_id:'item',operation:'add',location_id:1894,expected_layout_version:1,customer_id:136,product_id:3822,inventory_type:'finished',unit:'boxes',quantity:165,stock_date:'2026-09-29',inventory_code:'Z.001.000148',location_name:'L016-2层-2格'}]);
const [stocktakeBatchBusy,setStocktakeBatchBusy]=useState(false),[stocktakeRefreshRequired,setStocktakeRefreshRequired]=useState(false),[stocktakeBatchUncertain,setStocktakeBatchUncertain]=useState(false),[warehouseOperationMessage,setWarehouseOperationMessage]=useState(''),[stocktakeLastResult,setStocktakeLastResult]=useState([]),[stocktakeBatchIdempotencyKey,setStocktakeBatchIdempotencyKey]=useState('unchanged-retry-key');
const refreshDashboard=async()=>{}, operationKey=()=> 'next-key',inventoryUnitLabel=()=> '只',formatNumber=String,cancelStocktakeDrafts=()=>{},removeStocktakeDraftItem=()=>{};
const mutateJson=async(path,method,body)=>{const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const b=await r.json();if(!r.ok){const e=new Error(apiErrorMessage(b,r.status));e.status=r.status;throw e;}return b;};
${handler}
return <section aria-label="盘点调整页面草稿汇总">${body}</section>;
}createRoot(document.getElementById('app')).render(<App/>);`;
(async()=>{const result=await esbuild.build({stdin:{contents:app,loader:'tsx',resolveDir:src},bundle:true,write:false,format:'iife'});let success=false;const requests=[];
const server=http.createServer((req,res)=>{if(req.method==='POST'){let body='';req.on('data',b=>body+=b);req.on('end',()=>{requests.push(JSON.parse(body));res.setHeader('content-type','application/json');res.statusCode=success?200:422;res.end(JSON.stringify(success?{items:[]}:{detail:[{loc:['body','items',0,'stock_date'],type:'date_from_datetime_parsing',msg:'invalid date'}]}));});return;}res.setHeader('content-type',req.url==='/app.js'?'application/javascript':'text/html');res.end(req.url==='/app.js'?result.outputFiles[0].text:'<div id="app"></div><script src="/app.js"></script>');});
await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({channel:'chrome',headless:true});try{const page=await browser.newPage();page.on('dialog',d=>d.accept());await page.goto('http://127.0.0.1:'+server.address().port);const btn=page.getByRole('button',{name:'一次确认 1 条'});await btn.click();await page.getByRole('alert').waitFor();assert((await page.getByRole('alert').innerText()).includes('第1项·库存日期：请填写有效日期'));assert.equal(requests.length,1);assert(await btn.isEnabled());await btn.click();await page.waitForFunction(()=>!document.querySelector('button.confirm').disabled);assert.equal(requests.length,2);assert.deepEqual(requests[0],requests[1]);success=true;await btn.click();await page.waitForFunction(()=>document.querySelector('button.confirm').disabled);assert.equal(requests.length,3);assert((await page.getByRole('alert').innerText()).includes('整批成功'));console.log('PASS: visible error with rack panel absent, rejected draft retained, same-key retry, successful clear');}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);process.exitCode=1;});

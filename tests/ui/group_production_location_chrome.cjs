// Synthetic localhost only: no formal database, credentials or network forwarding.
const fs=require('fs'),path=require('path'),http=require('http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),out=process.env.ERP_UI_ARTIFACT_DIR||path.join(require('os').tmpdir(),'erp-group-location');fs.mkdirSync(out,{recursive:true});
let product={id:7,customer_id:1,version:2,product_code:'P007',product_name:'隔离测试纸箱',box_type:'普通箱',material_id:3,material_code:'R4',layer_count:3,flute_type:'B',length_mm:100,width_mm:100,height_mm:100,report_width_mm:100,report_length_mm:200,production_notes:'测试说明',sale_unit_price:'99',common_box_readiness:{ready:false,missing_fields:['材质']},drawings:[]};
const pallet={contract_version:'standard-pallet-v1',width_mm:1200,depth_mm:1000,height_mm:150};
const lot={lot_id:18,product_id:7,customer_id:1,inventory_type:'finished',inventory_code:'P007',product_name:'隔离测试纸箱',customer_name:'隔离客户',customer_short_name:'测试',quantity:29,physical_quantity:29,available_quantity:29,unit:'个',lot_number:'FG-TEST',status:'active',specification:'100×100×100'};
const location={location_id:92,location_code:'A2',location_name:'三楼 A2货架 2层1格',employee_location_name:'三楼 A2货架 2层1格',floor_code:'3F',area_code:'A',map_rack_id:'rack-1',map_feature_id:'zone-1',address_kind:'rack_slot',level_no:2,slot_no:1,source_version:'CURRENT_MAP',warehouse_type:'finished',storage_type:'rack',is_active:true,occupancy_status:'occupied',position_status:'mapped',map_position:{left_pct:20,top_pct:20,width_pct:6,height_pct:8,version:1,z_index:1,source_type:'manual',layout_kind:'logical_anchor'},loose_items:[lot],pallet:null,pallets:[]};
const rack={id:'rack-1',layout_id:'fixture',rack_code:'A2',name:'A2',x_mm:4000,y_mm:3000,z_mm:0,width_mm:3000,depth_mm:1000,height_mm:3000,levels:3,level_heights_mm:[1000,1000,1000],level_cell_counts:[2,2,2],cargo_rows:1,bays:2,rotation_deg:0,color:'#466875',access_side:'south',min_aisle_width_mm:1000,source:'manual',status:'confirmed',version:1};
const zone={id:'zone-1',layout_id:'fixture',feature_code:'A',name:'A区',feature_kind:'zone',subtype:'warehouse',points:[[1000,1000],[14000,1000],[14000,9000],[1000,9000]],storage_mode:'rack',elevation_mm:0,storage_height_mm:3000,color:'#dcf4e6',area_mm2:104000000,source:'manual',status:'confirmed',version:1,erp_area_code:'A',formal_binding_status:'bound',formal_policy_status:'published',formal_construction_status:'enabled',capacity_eligible:true};
const layout={layout_id:'fixture',name:'隔离仓库',floor_code:'3F',source_name:'synthetic',source_units:'mm',bounds_mm:{min_x:0,min_y:0,max_x:20000,max_y:12000,width:20000,height:12000},structures:[],features:[zone],placements:[],racks:[rack],assets:[],revision:'fixture-1',standard_pallet:pallet,projection_notice:'',alignment_status:'aligned',alignment_applied:true};
const dashboard={generated_at:'2026-09-12T15:00:00+08:00',read_only:true,standard_pallet:pallet,scope:{notice:''},summary:{active_lots:1,occupied_pallets:0,long_age_lots:0,unlocated_lots:0},floors:[{floor_code:'3F',occupied_locations:1,active_lots:1}],locations:[location,{...location,location_id:93,slot_no:2,location_name:'三楼 A2货架 2层2格',employee_location_name:'三楼 A2货架 2层2格',occupancy_status:'empty',loose_items:[]}],unlocated_inventory:[],distribution:{areas:[]}};
const html=fs.readFileSync(root+'/static/index.html','utf8').replace('        async mounted() {','        async fixtureDisabledMounted() {').replace('app.mount("#app");','window.erpFixture=app.mount("#app");');
const requests=[];
const server=http.createServer((req,res)=>{const u=new URL(req.url,'http://localhost');requests.push([req.method,u.pathname]);
 if(u.pathname.startsWith('/api/')){res.setHeader('Content-Type','application/json');let body={items:[],total:0,counts:{},permissions:[]};
  if(u.pathname==='/api/production/temporary-locations')body={items:[92,93].map(id=>({id,warehouse_floor:3,area_id:9,area_code:'EDIT-050',area_master_name:'南A2',map_rack_id:'rack-1',rack_code:'A',rack_display_name:'L001',storage_type:'rack',level_no:2,slot_no:id===92?1:2,location_name:'三楼 A2货架 2层'+(id===92?1:2)+'格',is_empty:id===93,layout_version:1}))};
  if(u.pathname==='/api/master/products/7'){if(req.method==='PUT')product={...product,common_box_readiness:{ready:true}};body=product;}
  if(u.pathname==='/api/auth/me')body={user:{id:1,role:'admin',ui_mode:'standard'},permissions:['warehouse.view','warehouse.execute','warehouse.correct','warehouse.stocktake.submit']};
  if(u.pathname==='/api/warehouse/twin-dashboard/overview')body=dashboard;
  if(u.pathname.startsWith('/api/warehouse/twin-layout/floors/'))body=layout;
  if(u.pathname==='/api/orders/cost-preview')body={cost_status:'calculated',estimated_cost:'1.2'};
  return res.end(JSON.stringify(body));
 }
 if(u.pathname==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');return res.end(html);}
 let relative=u.pathname;if(relative==='/warehouse.html')relative='/static/factory-twin-assets/warehouse-twin.html';if(relative.startsWith('/factory-twin-assets/'))relative='/static'+relative;
 const file=path.resolve(root,'.'+decodeURIComponent(relative));if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.statusCode=404;return res.end();}
 res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html; charset=utf-8':file.endsWith('.svg')?'image/svg+xml':'application/octet-stream');res.end(fs.readFileSync(file));
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({channel:'chrome',headless:true});const errors=[];
 try{const page=await browser.newPage({viewport:{width:1366,height:768}});page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')console.log(m.text())});await page.goto('http://127.0.0.1:'+server.address().port);await page.waitForFunction(()=>!!window.erpFixture);
 await page.evaluate(async()=>{const a=window.erpFixture;a.user={id:1,role:'admin',permissions:['*']};a.hasPermission=()=>true;a.pageAllowed=()=>true;a.activePage='production';a.productionTab='preparation';a.stockPrepBusy=false;a.authGeneration=1;
 Object.defineProperty(crypto,'randomUUID',{value:undefined,configurable:true});
 a.stockPrepDialog={row:{entry_type:'kit',children:[],plan:{recipe:{parent_id:205,code:'205',name:'组合网格',children:[{name:'长片',per_set:3,unit:'只'},{name:'短片',per_set:4,unit:'只'}]}}},sets:5,location:null,preview:{sets:5,basis_hash:'test',shortages:[],inputs:[],recipe:{children:[]}},loading:false,error:'',view:'action',page:1};await a.ensureProductionLocations();
 });
 await page.getByLabel('楼层',{exact:true}).selectOption('3');await page.getByLabel('区域大类').selectOption('A');await page.getByLabel('子区域').selectOption('9');await page.getByLabel('具体货位').selectOption('92');
 assert.equal(await page.evaluate(()=>window.erpFixture.stockPrepDialog.location),92);
 await page.getByRole('button',{name:'地图选位',exact:true}).click();const frame=page.frameLocator('iframe[title="生产成品地图选位"]');
 await frame.locator('.twin-workspace').waitFor();
 // Synthetic setup selects the physical rack through the actual search deep-link, then users select cells.
 const iframe=page.frames().find(f=>f.url().includes('production-location-picker'));const url=new URL(iframe.url());url.searchParams.set('location_id','92');url.searchParams.set('lot_id','18');await iframe.goto(url.toString());
 await frame.getByRole('button',{name:'确定此货位',exact:true}).waitFor();await page.screenshot({path:out+'/occupied-rack.png'});
 await frame.getByRole('button',{name:'确定此货位',exact:true}).click();await page.waitForFunction(()=>!window.erpFixture.stockLocationMap);assert.equal(await page.evaluate(()=>window.erpFixture.stockPrepDialog.location),92);
 await page.getByRole('button',{name:'地图选位',exact:true}).click();await frame.locator('.twin-workspace').waitFor();const iframe2=page.frames().find(f=>f.url().includes('production-location-picker'));const url2=new URL(iframe2.url());url2.searchParams.set('location_id','92');url2.searchParams.set('lot_id','18');await iframe2.goto(url2.toString());
 await frame.getByRole('button',{name:'确定此货位',exact:true}).waitFor();await frame.getByRole('button',{name:'三楼 A2货架 2层2格：查看货位',exact:true}).last().click();await page.screenshot({path:out+'/empty-rack.png'});await frame.getByRole('button',{name:'确定此货位',exact:true}).click();await page.waitForFunction(()=>!window.erpFixture.stockLocationMap);
 assert.deepEqual(await page.evaluate(()=>({location:window.erpFixture.stockPrepDialog.location,sets:window.erpFixture.stockPrepDialog.sets})),{location:93,sets:5});
 await page.screenshot({path:out+'/returned-plan.png'});await page.getByRole('button',{name:'整组转待生产',exact:true}).click();await page.waitForFunction(()=>!window.erpFixture.stockPrepDialog);
 assert.equal(requests.filter(([method,url])=>method==='POST'&&url.endsWith('/group-actions')).length,1);assert.equal(requests.filter(([method,url])=>method==='POST'&&url.startsWith('/api/warehouse/')).length,0);assert.deepEqual(errors,[]);
 fs.writeFileSync(out+'/result.json',JSON.stringify({ok:true,errors,requests},null,2));console.log('HTTP no randomUUID, hierarchy, occupied/empty rack selection, draft return and single submit passed');
 }catch(e){fs.writeFileSync(out+'/error.json',JSON.stringify({message:e.message,errors,requests},null,2));throw e;}finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

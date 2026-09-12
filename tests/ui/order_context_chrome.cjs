// Synthetic localhost only: no formal database, credentials or network forwarding.
const fs=require('fs'),path=require('path'),http=require('http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),out=process.env.ERP_UI_ARTIFACT_DIR||path.join(require('os').tmpdir(),'erp-order-context');fs.mkdirSync(out,{recursive:true});
let product={id:7,customer_id:1,version:2,product_code:'P007',product_name:'隔离测试纸箱',box_type:'普通箱',material_id:3,material_code:'R4',layer_count:3,flute_type:'B',length_mm:100,width_mm:100,height_mm:100,report_width_mm:100,report_length_mm:200,production_notes:'测试说明',sale_unit_price:'99',common_box_readiness:{ready:false,missing_fields:['材质']},drawings:[]};
const pallet={contract_version:'standard-pallet-v1',width_mm:1200,depth_mm:1000,height_mm:150};
const lot={lot_id:18,product_id:7,customer_id:1,inventory_type:'finished',inventory_code:'P007',product_name:'隔离测试纸箱',customer_name:'隔离客户',customer_short_name:'测试',quantity:29,physical_quantity:29,available_quantity:29,unit:'个',lot_number:'FG-TEST',status:'active',specification:'100×100×100'};
const location={location_id:92,location_code:'A2',location_name:'四楼 A2货架 2层1格',employee_location_name:'四楼 A2货架 2层1格',floor_code:'4F',area_code:'A',map_rack_id:'rack-1',map_feature_id:'zone-1',address_kind:'rack_slot',level_no:2,slot_no:1,source_version:'CURRENT_MAP',warehouse_type:'finished',storage_type:'rack',is_active:true,occupancy_status:'occupied',position_status:'mapped',map_position:{left_pct:20,top_pct:20,width_pct:6,height_pct:8,version:1,z_index:1,source_type:'manual',layout_kind:'logical_anchor'},loose_items:[lot],pallet:null,pallets:[]};
const rack={id:'rack-1',layout_id:'fixture',rack_code:'A2',name:'A2',x_mm:4000,y_mm:3000,z_mm:0,width_mm:3000,depth_mm:1000,height_mm:3000,levels:3,level_heights_mm:[1000,1000,1000],level_cell_counts:[2,2,2],cargo_rows:1,bays:2,rotation_deg:0,color:'#466875',access_side:'south',min_aisle_width_mm:1000,source:'manual',status:'confirmed',version:1};
const zone={id:'zone-1',layout_id:'fixture',feature_code:'A',name:'A区',feature_kind:'zone',subtype:'warehouse',points:[[1000,1000],[14000,1000],[14000,9000],[1000,9000]],storage_mode:'rack',elevation_mm:0,storage_height_mm:3000,color:'#dcf4e6',area_mm2:104000000,source:'manual',status:'confirmed',version:1,erp_area_code:'A',formal_binding_status:'bound',formal_policy_status:'published',formal_construction_status:'enabled',capacity_eligible:true};
const layout={layout_id:'fixture',name:'隔离仓库',floor_code:'4F',source_name:'synthetic',source_units:'mm',bounds_mm:{min_x:0,min_y:0,max_x:20000,max_y:12000,width:20000,height:12000},structures:[],features:[zone],placements:[],racks:[rack],assets:[],revision:'fixture-1',standard_pallet:pallet,projection_notice:'',alignment_status:'aligned',alignment_applied:true};
const dashboard={generated_at:'2026-09-12T15:00:00+08:00',read_only:true,standard_pallet:pallet,scope:{notice:''},summary:{active_lots:1,occupied_pallets:0,long_age_lots:0,unlocated_lots:0},floors:[{floor_code:'4F',occupied_locations:1,active_lots:1}],locations:[location],unlocated_inventory:[],distribution:{areas:[]}};
const html=fs.readFileSync(root+'/static/index.html','utf8').replace('        async mounted() {','        async fixtureDisabledMounted() {').replace('app.mount("#app");','window.erpFixture=app.mount("#app");');
const requests=[];
const server=http.createServer((req,res)=>{const u=new URL(req.url,'http://localhost');requests.push([req.method,u.pathname]);
 if(u.pathname.startsWith('/api/')){res.setHeader('Content-Type','application/json');let body={items:[],total:0,counts:{},permissions:[]};
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
 try{const page=await browser.newPage({viewport:{width:1366,height:768}});page.on('pageerror',e=>errors.push(e.message));await page.goto('http://127.0.0.1:'+server.address().port);await page.waitForFunction(()=>!!window.erpFixture);
  await page.evaluate(product=>{const a=window.erpFixture;a.user={id:1,role:'admin',permissions:['*']};a.hasPermission=()=>true;a.pageAllowed=()=>true;a.activePage='orders';a.customerOptions=[{id:1,name:'隔离客户'}];a.allCustomers=a.customerOptions;a.modal={type:'orderPdfImport',title:'PDF测试订单'};a.emailQueueMode=true;a.emailQueueFilter='ready';a.pdfQueueVisible=false;
   a.orderImportDrafts=[{source_name:'TEST.pdf',file_hash:'test-pdf',matched_customer_id:1,customer_po:'PO-TEST',email_queue_status:'ready',recognition_status:'recognized',confirmed:false,items:[{line_no:1,client_line_id:'line-1',normalized_product_code:'P007',raw_product_code:'P007',product_name:product.product_name,matched_product_id:7,quantity:30,unit_price:'2.35',_inventory_product:product,_common_box_readiness:product.common_box_readiness,_inventory:{loading:false,stale:false,finished:{candidates:[],allocations:[],selected_candidates:[]},semi:{whole:{candidates:[],allocations:[],selected_candidates:[],manual_candidates:[]}},authoritative:{coverage_state:'partial',order_quantity:30,finished_planned_quantity:29,production_required_quantity:1,requisition_sheet_quantity:1}}}]}];
   a._originalDraft=a.orderImportDrafts[0];a._originalLine=a.orderImportDrafts[0].items[0];a.inventoryDecisionRequired=()=>'';a.pdfInventoryDisplayState=()=> 'partial';
   a.openProduct=async()=>{a.productForm=a.hydrateProductForm(product);a.modal={type:'product'};a.productFormSnapshot=null;return true;};
   a.saveModal=async()=>{await axios.put('/api/master/products/7',{});await a.returnFromCommonBoxEditor(7,{saved:true});};
  },product);
  await page.getByRole('button',{name:'常用箱资料待完善',exact:true}).click();await page.getByRole('button',{name:'返回订单',exact:true}).waitFor();await page.screenshot({path:out+'/edit-product.png'});
  assert.equal(await page.evaluate(()=>window.erpFixture.orderContextOpening),false,'Vue proxy context must finish opening');
  await page.getByText('编辑常用箱 · P007',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>{const a=window.erpFixture;a.masterSavePending=true;a.closeModal();const kept=a.modal.type==='product'&&!!a.productEditReturnContext;a.masterSavePending=false;return kept;}),true,'pending product save cannot close its return context');
  await page.getByRole('button',{name:'保存并返回订单',exact:true}).click();await page.getByText('资料已完善',{exact:true}).first().waitFor();
  assert.deepEqual(await page.evaluate(()=>{const a=window.erpFixture;return {same:a.orderImportDrafts[0]===a._originalDraft&&a.orderImportDrafts[0].items[0]===a._originalLine,qty:a._originalLine.quantity,price:a._originalLine.unit_price};}),{same:true,qty:30,price:'2.35'});
  await page.evaluate(()=>{const a=window.erpFixture,item=a._originalLine;item._inventory.finished.candidates=[{lot_id:18,lot_number:'FG-TEST',quantity_available:29,warehouse_location:{id:92,warehouse_floor:4,position_status:'mapped',location_name:'四楼 A2货架 2层1格'}}];a.pdfOpenInventory(item);});
  await page.locator('.order-location-link').first().click();await page.locator('.pdf-warehouse-locator').waitFor();
  const frame=page.frameLocator('iframe[title="库存位置只读查看"]');await frame.getByText('黄色标记为当前货位',{exact:false}).waitFor({timeout:15000});
  await frame.locator('.rack-search-current').waitFor();await frame.locator('.shelf-product-label-button.selected').waitFor();
  assert.equal(await frame.getByRole('button',{name:'盘点',exact:true}).count(),0);
  assert.equal(await frame.locator('a[target="_top"]').count(),0);
  assert.equal(await frame.locator('.twin-context-rail').count(),0,'empty search results must not cover the located rack');
  const top=await page.evaluate(()=>{const box=document.querySelector('.pdf-warehouse-locator-head').getBoundingClientRect();return document.elementFromPoint(box.x+15,box.y+15)?.closest('.pdf-warehouse-locator')!==null;});assert.equal(top,true);
  await page.screenshot({path:out+'/located-stock.png'});await page.getByRole('button',{name:'返回 PDF 订单',exact:true}).click();await page.locator('.pdf-warehouse-locator').waitFor({state:'hidden'});await page.screenshot({path:out+'/returned-pdf.png'});
  assert.equal(await page.evaluate(()=>window.erpFixture._originalLine.unit_price),'2.35');assert.equal(await page.evaluate(()=>window.erpFixture._originalLine.quantity),30);
  await page.evaluate(product=>{const a=window.erpFixture;a._originalLine._show_inventory_details=false;a.modal={type:'order',title:'新建订单'};a.orderForm.customer_id=1;a.orderForm.items=[];
   const row={...product,common_box_readiness:{ready:false,missing_fields:['材质']}};a.orderCommonBoxPicker={...a.orderCommonBoxPicker,visible:true,items:[row],selected:{7:{product:row,quantity:'18',sequence:1}},total:1,page:2,pages:3,loading:false,filters:{product_code:'P007',product_name:'',spec:'100'}};
  },product);
  await page.locator('.order-common-box-table').getByRole('button',{name:'待完善',exact:true}).click();await page.getByRole('button',{name:'返回订单',exact:true}).click();
  assert.equal(await page.locator('.order-common-box-table input[type="number"]').inputValue(),'18');assert.equal(await page.locator('.order-common-box-table input[type="checkbox"]').isChecked(),true);
  await page.locator('.order-common-box-table').getByRole('button',{name:'待完善',exact:true}).click();await page.getByRole('button',{name:'保存并返回订单',exact:true}).click();await page.locator('.order-common-box-table').getByText('已完善',{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>{const p=window.erpFixture.orderCommonBoxPicker;return {qty:p.selected[7].quantity,page:p.page,code:p.filters.product_code,spec:p.filters.spec};}),{qty:'18',page:2,code:'P007',spec:'100'});
  await page.screenshot({path:out+'/returned-common-box-picker.png'});
  assert.deepEqual(errors,[]);assert.equal(requests.filter(([method,url])=>url.startsWith('/api/warehouse/')&&!['GET','HEAD'].includes(method)).length,0);
  fs.writeFileSync(out+'/result.json',JSON.stringify({ok:true,errors,requests},null,2));console.log('PDF and common-box picker edit/save/cancel/return; exact read-only rack/label navigation passed');
 }catch(e){fs.writeFileSync(out+'/error.json',JSON.stringify({message:e.message,errors,requests},null,2));throw e;}finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

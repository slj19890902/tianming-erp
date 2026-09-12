import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const html=fs.readFileSync(new URL('../../../static/index.html',import.meta.url),'utf8');
const script=fs.readFileSync(new URL('../../../static/ui/order-context.js',import.meta.url),'utf8');
function rootMethod(start,end){return html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start))).trim().replace(/,$/,'');}
function fixture(){
 const reads=[],writes=[],messages=[],refreshes=[],parts=[],components={};
 const focus={isConnected:true,focus(){this.focused=true;}},doc={activeElement:focus,querySelectorAll:()=>[],querySelector:()=>null};
 let product={id:7,customer_id:1,product_code:'P7',product_name:'更新产品',material_id:3,material_code:'R4',flute_type:'B',report_width_mm:100,production_notes:'新说明',sale_unit_price:'99',common_box_readiness:{ready:true},version:2};
 const axios={async get(url){reads.push(url);return {data:product};},async post(url,body){writes.push([url,body]);return {data:{cost_status:'calculated',estimated_cost:3}};}};
 const sandbox={axios,document:doc,console,URLSearchParams,window:{location:{href:'http://example.test/',origin:'http://example.test'},history:{pushState(){},back(){}}}};
 vm.runInNewContext(script,sandbox);sandbox.window.ERPOrderContext.install({component(n,d){components[n]=d;},mixin(d){parts.push(d);}});
 const ctx={...parts[0].methods,...vm.runInNewContext(`({${rootMethod('          async returnFromCommonBoxEditor(', '          async openProductStockPolicy(')},${rootMethod('          pdfWarehouseLocatorCanOpen(', '          inventorySourceLabel(')}})`,sandbox),
  user:{id:1},authGeneration:1,canEditProducts:true,canViewCosts:false,orderContextOpening:false,masterSavePending:false,
  modal:{type:'orderPdfImport',title:'原订单'},productForm:{},orderProductOptions:{},orderForm:{customer_id:1,items:[],remark:'不要改变'},orderImportDrafts:[],
  orderCommonBoxPicker:{visible:false,items:[],selected:{},page:2,filters:{spec:'100'}},pdfWarehouseLocator:{visible:false},
  $nextTick:fn=>Promise.resolve(fn?.()),$refs:{},hasPermission:()=>true,isImportDraftLocked:d=>!!d.locked,
  async openProduct(row){this.productForm={id:row.id};this.modal={type:'product'};return true;},
  commonBoxReadiness:p=>p._common_box_readiness||p.common_box_readiness||{ready:false},
  spec:p=>`100×${p.report_width_mm}`,orderSpecificationText:s=>s,materialName:()=>'',refreshPdfMaterialComparison(){},refreshPdfPriceConflict(){},
  invalidateLineInventory(line){line._inventory={stale:true};},invalidateImportDraftConfirmation(d){d.confirmed=false;},
  refreshOrderLineInventory(line,customer){refreshes.push([line,customer]);},resetProductEditorState(){},showToast:(...m)=>messages.push(m),errorMessage:e=>e.message,
  inventoryLocation:c=>c.warehouse_location?.location_name||'货位'};
 return {ctx,reads,writes,messages,refreshes,axios,focus,components,setProduct:p=>{product=p;},getProduct:()=>product};
}
function line(){return {matched_product_id:7,product_id:7,raw_product_code:'原PDF编码',client_line_id:'line-1',quantity:27,unit_price:'8.25',production_notes:'订单专用',drawing_file:'原图',bom_component_demands:[{id:4,quantity:13}],_inventory_product:{id:7,customer_id:1,material_id:3,material_code:'R4',flute_type:'B',report_width_mm:100},_inventory:{chosen:'keep'},_common_box_readiness:{ready:false}};}
test('PDF editor returns to the same email draft and refreshes only its matching product without rematching or changing quantities/prices',async()=>{
 const f=fixture(),a=line(),b={...line(),matched_product_id:8,product_id:8},draft={file_hash:'pdf-hash',preview_safety_token:'safety',email_document_id:12,confirmed:true,items:[a,b],_product_page:2};
 f.ctx.orderImportDrafts=[draft];const oldInventory=a._inventory,other=JSON.stringify(b),demands=a.bom_component_demands;
 assert.equal(await f.ctx.openOrderContextProduct(a,'pdf',draft),true);
 assert.equal(await f.ctx.returnFromCommonBoxEditor(7,{saved:true}),true);
 assert.equal(f.ctx.modal.type,'orderPdfImport');assert.equal(f.ctx.orderImportDrafts[0],draft);assert.equal(draft.items[0],a);assert.equal(a.quantity,27);assert.equal(a.unit_price,'8.25');assert.equal(a.production_notes,'订单专用');assert.equal(a.bom_component_demands,demands);
 assert.equal(a._inventory,oldInventory,'notes/readiness edits retain the explicit inventory choice');assert.equal(a._common_box_readiness.ready,true);assert.equal(JSON.stringify(b),other);assert.equal(draft.preview_safety_token,'safety');assert.equal(draft.email_document_id,12);assert.equal(draft._product_page,2);assert.equal(draft.confirmed,false);
 assert.deepEqual(f.reads,['/api/master/products/7']);assert.equal(f.writes.length,0);assert.equal(f.refreshes.length,0);
});
test('new order and picker preserve selected quantities, filter/page and overrides; changed material dimensions recheck only that product',async()=>{
 const f=fixture(),a=line(),draft=f.ctx.orderForm;draft.items=[a,{product_id:8,quantity:10}];f.ctx.modal={type:'order'};
 const selected={quantity:'18',sequence:5,product:{id:7}};f.ctx.orderCommonBoxPicker={visible:true,items:[{id:7}],selected:{7:selected},page:3,filters:{spec:'100'}};
 f.setProduct({...f.getProduct(),report_width_mm:120});
 await f.ctx.openCommonBoxEditorFromPicker({id:7});assert.equal(f.ctx.orderCommonBoxPicker.visible,false);
 await f.ctx.returnFromCommonBoxEditor(7,{saved:true});
 assert.equal(f.ctx.orderForm,draft);assert.equal(a.quantity,27);assert.equal(a.unit_price,'8.25');assert.equal(a.drawing_file,'原图');assert.equal(selected.quantity,'18');assert.equal(selected.sequence,5);assert.equal(selected.product.report_width_mm,120);assert.equal(f.ctx.orderCommonBoxPicker.visible,true);assert.equal(f.ctx.orderCommonBoxPicker.page,3);assert.equal(f.ctx.orderCommonBoxPicker.filters.spec,'100');assert.equal(f.refreshes.length,1);assert.equal(f.refreshes[0][0],a);
});
test('cancel and failed product opening restore the source without clearing picker selections or making writes',async()=>{
 const f=fixture();f.ctx.modal={type:'order'};f.ctx.orderCommonBoxPicker.visible=true;f.ctx.orderCommonBoxPicker.selected={7:{quantity:'4'}};
 f.ctx.openProduct=async()=>false;
 assert.equal(await f.ctx.openCommonBoxEditorFromPicker({id:7}),false);assert.equal(f.ctx.modal.type,'order');assert.equal(f.ctx.productEditReturnContext,null);assert.equal(f.ctx.orderCommonBoxPicker.visible,true);assert.equal(f.ctx.orderCommonBoxPicker.selected[7].quantity,'4');assert.equal(f.reads.length,0);
});
test('failed refresh after save stays in the editor; return retries the read without resaving or claiming ready',async()=>{
 const f=fixture(),a=line();f.ctx.orderForm.items=[a];f.ctx.modal={type:'order'};
 await f.ctx.openOrderContextProduct(a,'order');f.axios.get=async()=>{throw Error('offline');};
 assert.equal(await f.ctx.returnFromCommonBoxEditor(7,{saved:true}),false);assert.equal(f.ctx.modal.type,'product');assert.equal(a._common_box_readiness.ready,false);assert.equal(f.ctx.productEditReturnContext.saved,true);
 f.axios.get=async()=>({data:f.getProduct()});assert.equal(await f.ctx.returnFromCommonBoxEditor(7,{saved:false}),true);assert.equal(a._common_box_readiness.ready,true);assert.equal(f.writes.length,0);
});
test('permissions, locked PDFs and stale session responses cannot reopen or refresh the order',async()=>{
 const f=fixture(),a=line(),draft={items:[a],locked:true};f.ctx.orderImportDrafts=[draft];
 assert.equal(await f.ctx.openOrderContextProduct(a,'pdf',draft),false);draft.locked=false;f.ctx.canEditProducts=false;
 assert.equal(await f.ctx.openOrderContextProduct(a,'pdf',draft),false);f.ctx.canEditProducts=true;await f.ctx.openOrderContextProduct(a,'pdf',draft);
 let resolve;f.axios.get=()=>new Promise(r=>{resolve=r;});const pending=f.ctx.returnFromCommonBoxEditor(7,{saved:true});f.ctx.authGeneration++;f.ctx.modal=null;resolve({data:f.getProduct()});
 assert.equal(await pending,false);assert.equal(f.ctx.modal,null);assert.equal(a._common_box_readiness.ready,false);
});
test('location link carries the actual floor and lot; return keeps the original order and restores focus',async()=>{
 const f=fixture(),draft=f.ctx.orderForm;f.ctx.modal={type:'order'};
 const candidate={lot_id:18,warehouse_location:{id:92,warehouse_floor:4,position_status:'mapped',location_name:'四楼 A2'}};
 f.ctx.openPdfWarehouseLocator(null,null,candidate);const url=new URL(f.ctx.pdfWarehouseLocator.url,'http://example.test');assert.equal(url.pathname,'/warehouse.html');
 assert.equal(url.searchParams.get('floor'),'4F');assert.equal(url.searchParams.get('location_id'),'92');assert.equal(url.searchParams.get('lot_id'),'18');assert.equal(url.searchParams.get('readonly'),'1');assert.equal(url.searchParams.get('source'),'order-context');assert.equal(url.searchParams.get('tab'),'map');
 f.ctx.closePdfWarehouseLocator(true);await Promise.resolve();assert.equal(f.ctx.orderForm,draft);assert.equal(f.ctx.modal.type,'order');assert.equal(f.focus.focused,true);
 f.ctx.openPdfWarehouseLocator(null,null,{...candidate,warehouse_location:{...candidate.warehouse_location,position_status:'unmapped'}});assert.match(f.ctx.pdfWarehouseLocator.url,/warehouse-ledger\.html/);
 f.ctx.closePdfWarehouseLocator(true);f.ctx.hasPermission=()=>false;f.ctx.openPdfWarehouseLocator(null,null,candidate);assert.equal(f.ctx.pdfWarehouseLocator.visible,false);
});

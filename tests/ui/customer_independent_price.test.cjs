const {test}=require('node:test');
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const html=fs.readFileSync(require.resolve('../../static/index.html'),'utf8');

function harness(){
  const requests=[], box={axios:{post:async(url,body)=>{requests.push({url,body});return {data:{can_update:true,current_version:7,confirmation_token:'bound-price-token'}};}}};
  vm.createContext(box);
  vm.runInContext(fs.readFileSync(require.resolve('../../static/js/product-structure-edit.js'),'utf8'),box);
  box.window=box;
  function actual(name){
    const m=html.match(new RegExp('^          (?:async )?'+name+'\\([^\\n]*\\) \\{','m'));
    assert.ok(m,name);
    const end=html.indexOf('\n          },',m.index);
    return vm.runInContext('({'+html.slice(m.index,end+13).trim().replace(/,$/,'')+'})',box)[name];
  }
  const c={
    productForm:{id:3366,version:7,customer_id:137,product_code:'80011641',product_name:'合成客户产品',
      box_category:'normal',box_style:'BOM组合',unit:'套',sale_unit_price:'5',material_id:null,
      report_length_mm:null,report_width_mm:null,length_mm:null,width_mm:null,height_mm:null,
      supply_mode:'corrugated_production',_production_processes:[],_structure_edit_fields:[],
      _external_selected_ids:[],production_label_enabled:true,production_label_units_per_label:50},
    drawingFile:null,bomDirty:false,storageDirty:false,loading:false,
    _productBomDirty(){return this.bomDirty;},receiptStorageDirty(){return this.storageDirty;},
    productPrintingWriteFields(){return {print_content:null,printing_colors:null,printing_plate_mode:'no_plate'};},
    masterCurrentForm(){return this.productForm;},masterCurrentVersion(){return this.productForm.version;},
    validateProductCreaseAndReport(){throw new Error('physical validation retained');},
    productMoldError:'existing mold needs review',productPrintingPlateError:'existing plate needs review',
    productPrintingColorError:'existing colors need review',productProductionLabelError:'existing label needs review',
    closeModal(){throw new Error('unexpected close');},showToast(){},handleMaster409(){return false;},
    errorMessage:error=>error.message,
  };
  for(const name of ['_productFormSaveFields','productOnlySalePriceChange','buildProductWritePayload',
    'attachMasterUpdateMetadata','prepareProductOneClickSave','productSaveBlockReason']) c[name]=actual(name);
  c.modal={type:'product'};c.canEditProducts=true;
  c.productFormSnapshot=JSON.stringify(c._productFormSaveFields());
  return {c,requests};
}

test('an existing customer price correction bypasses unrelated BOM/material checks and keeps only the intended mutation',()=>{
  const {c}=harness();c.productForm.sale_unit_price='8.123456';
  assert.equal(c.productOnlySalePriceChange(),true);
  const body=c.buildProductWritePayload({expected_version:7,confirmation_token:'confirmed'});
  assert.deepEqual(JSON.parse(JSON.stringify(body)),{customer_id:137,product_code:'80011641',product_name:'合成客户产品',
    box_category:'normal',sale_unit_price:'8.123456',structure_edit_fields:[],expected_version:7,confirmation_token:'confirmed'});
  assert.equal(c.productForm.material_id,null);assert.equal(c.productForm.unit,'套');
});

test('explicitly clearing a customer price sends null while zero remains a real price',()=>{
  const {c}=harness();c.productForm.sale_unit_price='';
  assert.equal(c.buildProductWritePayload().sale_unit_price,null);
  c.productForm.sale_unit_price='0';assert.equal(c.buildProductWritePayload().sale_unit_price,'0');
});

test('actual production and customer ownership changes cannot be swallowed by the local route',()=>{
  const changes=[['material_id',71],['report_length_mm',802],['width_mm',400],['unit','片'],
    ['customer_id',138],['production_notes','新增实际工艺'],
    ['production_label_units_per_label',100],['_production_processes',['模切']],
    ['_structure_edit_fields',['production_process']],['sheet_cutting_settings',{schema:2}],
    ['supplier_edge_type','净料']];
  for(const [field,value]of changes){
    const {c}=harness();c.productForm.box_style='normal';c.productFormSnapshot=JSON.stringify(c._productFormSaveFields());
    c.productForm.sale_unit_price='8';c.productForm[field]=value;
    assert.equal(c.productOnlySalePriceChange(),false,field);
    assert.throws(()=>c.buildProductWritePayload(),/physical validation retained/,field);
  }
});

test('customer code/name/document corrections use the local payload despite unrelated old BOM gaps',()=>{
  for(const field of ['product_code','product_name','customer_material_code','customer_drawing_number']) {
    const {c}=harness();c.productForm[field]='CUSTOMER-ONLY';
    assert.equal(c.productOnlySalePriceChange(),true,field);
    assert.equal(c.buildProductWritePayload()[field],'CUSTOMER-ONLY');
    assert.equal(c.buildProductWritePayload().material_id,undefined);
    assert.equal(c.productSaveBlockReason(),'');
    c.canEditProducts=false;
    assert.equal(c.productSaveBlockReason(),'当前账号没有编辑权限');
  }
});

test('related drawings, BOM, location writes and an unknown/new baseline retain the full save path',()=>{
  for(const configure of [c=>c.drawingFile={name:'drawing.pdf'},c=>c.bomDirty=true,c=>c.storageDirty=true,
    c=>c.productFormSnapshot=null,c=>c.productFormSnapshot='invalid JSON',c=>c.productForm.id=null]){
    const {c}=harness();c.productForm.sale_unit_price='8';configure(c);
    assert.equal(c.productOnlySalePriceChange(),false);
  }
  const {c}=harness();assert.equal(c.productOnlySalePriceChange(),false);
});

test('the real one-click preflight submits the same customer-specific price payload and returns bound version/token',async()=>{
  const {c,requests}=harness();c.productForm.sale_unit_price='8.123456';
  const result=await c.prepareProductOneClickSave();
  assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/master/products/3366/update-preview');
  assert.equal(requests[0].body.sale_unit_price,'8.123456');assert.equal(requests[0].body.expected_version,7);
  assert.equal(requests[0].body.material_id,undefined);
  assert.equal(result.options.expected_version,7);assert.equal(result.options.confirmation_token,'bound-price-token');
  assert.equal(c.loading,false);
});

const {test}=require('node:test');
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const html=fs.readFileSync(require.resolve('../../static/index.html'),'utf8');

function harness({bom=false,dirty=true,fail=false}={}) {
  const requests=[], messages=[];
  const box={axios:{put:async(url,body)=>{
    requests.push({url,body});
    if(fail)throw Error('保存失败');
    const product={id:1,version:8,receipt_storage:{product_id:1,version:2,area_id:3,location_id:4,shared_group_version:5}};
    return {data:bom?{product,bom:{}}:product};
  }}};
  vm.createContext(box);
  function actual(name){
    const m=html.match(new RegExp('^          (?:async )?'+name+'\\([^\\n]*\\) \\{','m'));
    assert.ok(m,name);const end=html.indexOf('\n          },',m.index);
    return vm.runInContext('({'+html.slice(m.index,end+13).trim().replace(/,$/,'')+'})',box)[name];
  }
  const c={modal:{type:'product'},productForm:{id:1,version:7,box_style:bom?'BOM组合':'A1'},
    receiptStorage:{product_id:1,version:1,shared_group_version:4,floor:3,area_id:3,location_id:4,
      saved_floor:3,saved_area_id:3,saved_location_id:2,locations:[{location_id:4,address_version:2,layout_version:8}]},
    bomEditor:{},productFormSnapshot:'initial',productEditReturnContext:null,drawingFile:null,
    masterChangeConfirm:{},masterCurrentForm(){return this.productForm;},masterCurrentVersion:()=>7,
    masterLocalChanges:()=>dirty?[{field:'box_style'}]:[],_productFormDirty:()=>dirty,
    _productBomDirty:()=>bom,productOnlySalePriceChange:()=>false,validateProductBom:()=>'',
    async prepareProductOneClickSave(){return {completed:false,options:{expected_version:7}};},
    buildProductWritePayload(){return {expected_version:7,receipt_storage:this.receiptStoragePayload(false)};},
    bomPayload:()=>({expected_version:7,inventory_mode:'assembled',components:[{component_product_id:2}]}),
    hydrateProductForm:data=>({...data}),beginMasterEdit(){},applyBomResponse(){},_productFormSaveFields:()=>({}),
    loadProducts:async()=>true,closeModal(){this.closed=true;},showToast(message){messages.push(message);},
    handleMaster409:()=>false,errorMessage:error=>error.message,
    saveReceiptStorage(){throw Error('must not save location before the product transaction');},
  };
  for(const name of ['saveModal','receiptStorageDirty','receiptStoragePayload','applyReceiptStorageSaved']) c[name]=actual(name);
  return {c,requests,messages,actual};
}

for(const bom of [false,true])test(`actual save sends location in the same ${bom?'BOM':'ordinary'} request`,async()=>{
  const {c,requests}=harness({bom});
  assert.equal(await c.saveModal(),true);
  assert.equal(requests.length,1);
  assert.equal(requests[0].url,`/api/master/products/1${bom?'/with-bom':''}`);
  const product=bom?requests[0].body.product:requests[0].body;
  assert.equal(product.receipt_storage.location_id,4);
  assert.equal(product.receipt_storage.expected_shared_group_version,4);
  assert.equal(c.receiptStorage.saved_location_id,4);
  assert.equal(c.receiptStorage.shared_group_version,5);
  assert.equal(c.masterSavePending,false);
});

test('only changing storage still submits the product transaction',async()=>{
  const {c,requests}=harness({dirty:false});
  assert.equal(await c.saveModal(),true);
  assert.equal(requests.length,1);
  assert.equal(requests[0].body.receipt_storage.location_id,4);
});

test('failure keeps unsaved destination and product edits without claiming success',async()=>{
  const {c,requests,messages}=harness({bom:true,fail:true});
  assert.equal(await c.saveModal(),false);
  assert.equal(requests.length,1);
  assert.equal(c.receiptStorage.location_id,4);
  assert.equal(c.receiptStorage.saved_location_id,2);
  assert.equal(c.productForm.version,7);
  assert.equal(c.closed,undefined);
  assert.equal(c.masterSavePending,false);
  assert.ok(messages.includes('保存失败'));
});

test('the real BOM payload maps the ordinary editor mode to null for HTTP validation',()=>{
  const {c,actual}=harness();
  c.bomEditor={enabled:false,inventory_mode:'legacy',expected_version:7,components:[]};
  c._productBomSaveFields=actual('_productBomSaveFields');
  c.bomPayload=actual('bomPayload');
  assert.equal(c.bomPayload().inventory_mode,null);
  assert.equal(c.bomPayload().components.length,0);
});

"""Cold customer-page entry and the shipped sharing save/readback methods."""
import json
from pathlib import Path
import shutil
import subprocess


SOURCE = Path('static/index.html').read_text()


def run_node(tmp_path, scenario):
    methods = '\n'.join([
        SOURCE[SOURCE.index('          async loadCustomers()'):SOURCE.index('          async exportCustomerPhoneGovernance()')],
        SOURCE[SOURCE.index('          async loadCustomerOptions('):SOURCE.index('          async loadProducts()')],
        SOURCE[SOURCE.index('          sharingDocumentCustomers()'):SOURCE.index('          async loadCustomerFinishedStorage(')],
    ])
    program = """
const assert=require('node:assert/strict'), vm=require('node:vm');
const calls=[],latestRequestControllers=new Map();let failOptions=false;
let settings={members:[],shared_source_product_ids:{},new_products_shared:false};
const customers=[{id:1,name:'目标客户',is_active:true},{id:2,name:'来源客户',is_active:true},{id:3,name:'停用客户',is_active:false}];
const api={get:async(url,config)=>{
 calls.push({url,config});
 if(url==='/api/master/customers'){
  if(failOptions && config.params.page_size===200)throw Error('客户选项读取失败');
  if(config.params.page_size!==200)return {data:{items:[customers[0]],total:3}};
  return {data:{items:config.params.page===1?[customers[0]]:customers.slice(1),total_pages:2,page:config.params.page}};
 }
 if(url.endsWith('/products'))return {data:[{id:21,code:'P21',name:'成品'}]};
 return {data:JSON.parse(JSON.stringify(settings))};
},post:async(url,payload)=>{
 calls.push({url,payload});
 if(url.endsWith('/preview'))return {data:{preview_hash:'checked-version',actions:[{source_id:21}],blocked:[]}};
 assert.equal(payload.preview_hash,'checked-version');assert.equal(payload.source_customer_id,2);
 settings={members:customers.slice(0,2),shared_source_product_ids:{2:[21]},new_products_shared:true};
 return {data:{linked:[{source_id:21}],blocked:[]}};
}};
const sandbox={axios:api,latestRequestControllers,console};
vm.runInNewContext('methods=({'+METHODS+'});',sandbox);
function blank(){return {busy:false,error:'',loadedId:null,source_customer_id:null,scope:'all',selected:[],products:[],members:[],new_products_shared:false,delivery_heading_customer_id:null,delivery_address_customer_id:null,preview:null,saved:''};}
function context(){
 const c={...sandbox.methods,user:{id:1},authGeneration:1,customerOptions:[],customerOptionsIdentity:'',customers:[],customerForm:{id:1},customerSharing:blank(),filters:{customerKeyword:'',showInactive:false},pages:{customers:1},
 hasPermission:()=>true,desktopListPageSize:()=>10,errorMessage:e=>e.message,showToast(){},isCancelledRequest:()=>false,
 beginLatestRequest:key=>{const v={signal:{}};latestRequestControllers.set(key,v);return v;},finishLatestRequest(){}};
 Object.defineProperty(c,'activeCustomerOptions',{get:()=>c.customerOptions.filter(row=>row.is_active)});return c;
}
SCENARIO
""".replace('METHODS', json.dumps(methods)).replace('SCENARIO', scenario)
    path=tmp_path/'sharing.js';path.write_text(program)
    result=subprocess.run([shutil.which('node'),str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


def test_cold_customer_page_loads_all_options_and_saves_then_reopens(tmp_path):
    run_node(tmp_path,"""
(async()=>{
 const c=context();await c.loadCustomers();assert.equal(c.customers.length,1);assert.equal(c.customerOptions.length,0);
 await c.loadCustomerSharing();
 assert.equal(c.activeCustomerOptions.filter(row=>row.id!==c.customerForm.id).map(row=>row.id).join(','),'2','cold customer entry never loaded source customers');
 c.customerSharing.source_customer_id=2;await c.loadSharingProducts();c.customerSharing.scope='selected';c.customerSharing.selected=[21];c.customerSharing.new_products_shared=true;
 assert.equal(c.customerSharing.products.length,1);
 await c.saveCustomerSharing();
 assert.equal(calls.filter(r=>r.url.endsWith('/confirm')).length,1,'save without a prior manual preview must submit the checked setting');
 assert.match(c.customerSharing.saved,/已保存/);
 c.customerSharing=blank();await c.loadCustomerSharing();
 assert.equal(c.customerSharing.source_customer_id,2);assert.equal(c.customerSharing.selected.join(','),'21');assert.equal(c.customerSharing.new_products_shared,true);
 assert.equal(c.sharingDocumentCustomers().map(r=>r.id).join(','),'2');
})().catch(e=>{console.error(e);process.exit(1)});
""")


def test_option_load_failure_is_visible_and_retryable_and_no_source_cannot_save(tmp_path):
    run_node(tmp_path,"""
(async()=>{
 const c=context();failOptions=true;await c.loadCustomerSharing();
 assert.match(c.customerSharing.error,/客户/);assert.equal(c.customerSharing.loadedId,null);assert.equal(c.customerSharing.busy,false);
 failOptions=false;await c.loadCustomerSharing(true);assert.equal(c.customerSharing.loadedId,1);assert.equal(c.customerSharing.error,'');
 await c.saveCustomerSharing();assert.equal(calls.filter(r=>r.payload).length,0);assert.match(c.customerSharing.error,/选择.*客户/);
})().catch(e=>{console.error(e);process.exit(1)});
""")


def test_reopening_same_customer_does_not_accept_an_old_dialog_response(tmp_path):
    run_node(tmp_path,"""
(async()=>{
 const c=context();await c.loadCustomerOptions();let finish;
 const original=api.get;api.get=(url,cfg)=>url.endsWith('/product-sharing')?new Promise(r=>{finish=r;}):original(url,cfg);
 const pending=c.loadCustomerSharing();while(!finish)await Promise.resolve();
 const fresh=blank();fresh.error='new dialog';c.customerSharing=fresh;
 finish({data:{members:customers.slice(0,2),shared_source_product_ids:{2:[21]}}});await pending;
 assert.equal(c.customerSharing,fresh);assert.equal(fresh.error,'new dialog');assert.equal(fresh.loadedId,null);
})().catch(e=>{console.error(e);process.exit(1)});
""")


def test_sharing_dropdown_is_not_clipped_and_save_is_available_before_preview():
    section=SOURCE.split('<details id="customer-sharing-section"',1)[1].split('</details>',1)[0]
    assert 'customer-sharing-panel' in section
    assert '.customer-sharing-panel { overflow:visible;' in SOURCE
    assert 'v-if="customerSharing.preview" class="btn primary"' not in section
    assert '重新读取' in section

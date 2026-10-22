const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('static/index.html','utf8');
const start=html.indexOf('<section v-if="productionTab===\'assembly\'"');
const template=html.slice(start,html.indexOf('</section>',start)+10);
const box={console:{warn(){},error(){}}};vm.createContext(box);
vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),box);
const render=box.Vue.compile(template,{decodeEntities:s=>s,onError:e=>{throw e;}});
function all(node){return node&&typeof node==='object'?[node,...(Array.isArray(node.children)?node.children.flatMap(all):[])]:[];}
function text(node){return typeof node==='string'?node:Array.isArray(node?.children)?node.children.map(text).join(''):typeof node?.children==='string'?node.children:'';}
function renderRows(admin=true){
 const rows=[{key:'stock:3',source_kind:'stock',customer_name:'研光',product_code:'80012753',product_name:'TRD-FR',parent_product_id:3,unit:'套',verified_available_sets:0,assembly_block:'尚缺子件',children:[{product_id:4,product_name:'A片',product_code:'80012753',quantity:2,unit:'片'}],sources:[{lot_id:7,product_id:4,product_code:'80012753',product_name:'A片',quantity:2,unit:'片',location:'三楼 I1-03'}],excluded_sources:[],outputs:[]},
 {key:'order:1',source_kind:'order',order_item_id:1,customer_name:'客户',product_code:'P2',product_name:'配套',outputs:[{product_id:2,product_name:'配套',quantity:3,unit:'套',location_id:4}],expected_outputs:{2:3},sources:[],children:[]}];
 const context={productionTab:'assembly',pendingAssemblies:rows,assemblyPage:1,assemblyTotal:2,assemblyLoading:false,assemblyError:'',assemblyLocations:[],productionBusy:false,canAdmin:admin,canEditProducts:admin,canProductionExecute:admin,hasPermission:()=>admin};
 return all(render(context,[])).filter(n=>n.type==='details'&&n.props?.class==='assembly-pending-row');
}
test('every pending item has one collapsed native summary with pairing data inside',()=>{
 const rows=renderRows();assert.equal(rows.length,2);
 for(const row of rows){assert.equal(row.props.open,undefined);const summaries=row.children.filter(n=>n.type==='summary');assert.equal(summaries.length,1);assert.ok(!all(summaries[0]).some(n=>['button','input','select','details'].includes(n.type)));}
 assert.match(text(rows[0].children[0]),/研光.*80012753.*TRD-FR.*可组 0 套/);
 assert.doesNotMatch(text(rows[0].children[0]),/A片|I1-03/);
 assert.match(text(rows[0]),/A片.*三楼 I1-03/);
 assert.ok(all(rows[1]).some(n=>n.type==='input'&&n.props.max===3));
 assert.match(text(rows[1]),/组套完成并入库/);
});
test('expanding a row does not expose execute controls to viewers',()=>{
 for(const row of renderRows(false))assert.ok(!all(row).some(n=>n.type==='button'));
});
test('production navigation retains pending production and removes duplicate incoming entry',()=>{
 const strip=html.slice(html.indexOf('<div class="production-compact-command-bar">'),html.indexOf('<section v-if="productionTab===\'assembly\'"'));
 assert.doesNotMatch(strip,/待收料|go\('incoming'\)/);assert.match(strip,/selectStockProductionTab\('pending'\)/);
});

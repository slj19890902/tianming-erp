const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('static/index.html','utf8');
const start=html.indexOf('async loadMoreProductPurchaseTraces() {');
const end=html.indexOf('          productMaterialCandidateKey(',start);
const method=html.slice(start,end).trim().replace(/,$/,'').replace('async loadMoreProductPurchaseTraces()','async function()');
function harness(response){
 const controllers=new Map();const sandbox={axios:{get:response},latestRequestControllers:controllers,Set,Number};
 const load=vm.runInNewContext('('+method+')',sandbox);
 const context={purchase_trace_next_before_id:10,purchase_trace_has_more:true,purchase_trace_loading:false,material_history:[{source_type:'official',document_id:10},{source_type:'purchase_workbook',document_id:10}]};
 const owner={productForm:{id:1},productMaterialContext:context,beginLatestRequest(k){const c={};controllers.set(k,c);return c;},finishLatestRequest(){},isCancelledRequest(){return false},errorMessage(e){return e.message}};
 return {load,owner,context};
}
(async()=>{
 const normal=harness(async()=>({data:{items:[{source_type:'purchase_workbook',document_id:10},{source_type:'purchase_workbook',document_id:9}],has_more:false,next_before_id:null}}));
 assert.equal(await normal.load.call(normal.owner),true);assert.equal(normal.context.material_history.length,3);assert.equal(normal.context.material_history[0].source_type,'official');
 for(const change of ['product','refresh']){
  let resolve;const race=harness(()=>new Promise(r=>resolve=r));const pending=race.load.call(race.owner);
  if(change==='product')race.owner.productForm.id=2;
  race.owner.productMaterialContext={material_history:[]};resolve({data:{items:[{document_id:9}],has_more:false}});
  assert.equal(await pending,false);assert.equal(race.owner.productMaterialContext.material_history.length,0);
 }
 const failed=harness(async()=>{throw Error('offline')});assert.equal(await failed.load.call(failed.owner),false);assert.equal(failed.context.material_history.length,2);assert.equal(failed.context.purchase_trace_next_before_id,10);assert.equal(failed.context.purchase_trace_error,'offline');
 let scripts=0;for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)){if(!/\bsrc\s*=/.test(match[1])&&match[2].trim()&&!/application\/json/.test(match[1])){new vm.Script(match[2]);scripts++;}}
 assert(html.includes('本行合计采购'));assert(html.includes('历史采购台账'));
 console.log(JSON.stringify({behavior_checks:4,inline_scripts_parsed:scripts,status:'passed'}));
})().catch(e=>{console.error(e);process.exit(1)});

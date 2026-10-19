const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
const workspace=fs.readFileSync(require.resolve('../../static/ui/production-workspace.js'),'utf8');
const html=fs.readFileSync(require.resolve('../../static/production-packaging-label.html'),'utf8');
function desk(){let mixin;const opens=[];const context={open:(...a)=>{opens.push(a);return {};}};vm.runInNewContext(workspace,{window:context});context.ERPProductionWorkspace.install({component(){},mixin(m){mixin=m;}});const state={...mixin.data.call({}),productionPlacement:[{inventory_lot_id:4,inventory_version:2},{inventory_lot_id:7,inventory_version:1}],productionPlacementLoading:false,hasPermission:()=>true};for(const [k,fn]of Object.entries(mixin.methods))state[k]=fn.bind(state);return {state,mixin,opens};}
const pkg={template_version:'current_40x30_v2',plan_fingerprint:'a'.repeat(64),lot_versions:[{lot_id:4,expected_version:2}],plans:[{selection_key:'finished-lot:4',label_count:2}],labels:[],label_count:2};
function page(search='?finished_stock=1&finished_lots=4:2'){
 const {window}=parseHTML(html);window.location={search,pathname:'/production-packaging-label.html'};window.confirm=()=>true;
 const calls=[];let requests=async(url,opt)=>({ok:true,json:async()=>({job_id:12,job:{id:12,status:'prepared'},package:pkg})});
 const ctx={window,document:window.document,URLSearchParams,AbortController,console,requestAnimationFrame:r=>r(),fetch:async(url,opt)=>{calls.push({url,opt});return requests(url,opt);}};
 const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.includes('async function preparePrintJob'));
 vm.runInNewContext(script.replace('      loadPackage();',`window.labelTest={loadPackage,preparePrintJob,confirmPrinted,updateLatestJobLink,set(p,latest=false){loadedPackage=plannedPackage=p;printCounts=new Map(p.plans.map(r=>[r.selection_key,r.label_count]));finishedLatestPrinted=latest;},jobs(j){loadedJobs=j;}};`),ctx);
 return {window,calls,api:window.labelTest,request:fn=>requests=fn};
}
test('selection uses lot version, page-only all and auth/page reset; loading cannot print',()=>{
 const {state:s,mixin,opens}=desk();s.toggleAllFinishedLabels(true);assert.equal(s.selectedFinishedLabelRows().length,2);s.productionPlacement[0].inventory_version++;assert.equal(s.selectedFinishedLabelRows().length,1);
 s.openFinishedProductLabels();assert.match(opens[0][0],/finished_lots=7%3A1/);s.productionPlacementLoading=true;s.openFinishedProductLabels();assert.equal(opens.length,1);
 mixin.watch.authGeneration.call(s);assert.equal(s.selectedFinishedLabelRows().length,0);s.productionPlacementLoading=false;s.toggleAllFinishedLabels(true);mixin.watch.productionTab.call(s);assert.equal(s.selectedFinishedLabelRows().length,0);
});
test('finished preview routes, exact selections/counts, confirmation and historical source flag',async()=>{
 const h=page();h.api.set(pkg);await h.api.preparePrintJob();assert.equal(h.calls[0].url,'/api/production/finished-product-labels/jobs');const body=JSON.parse(h.calls[0].opt.body);assert.deepEqual(body.selections,[{lot_id:4,expected_version:2}]);assert.deepEqual(body.items,[{selection_key:'finished-lot:4',print_label_count:2}]);
 h.api.jobs([{id:12,status:'prepared'}]);await h.api.confirmPrinted();assert.equal(h.calls[1].url,'/api/production/finished-product-labels/jobs/12/confirm');
 h.api.updateLatestJobLink({latestPrintedJob:{id:12,template_version:'current_40x30_v2'}});assert.match(h.window.document.querySelector('#latestJobLink').getAttribute('href'),/finished_stock=1/);
 const live=page();await live.api.loadPackage();assert.match(live.calls[0].url,/finished-product-labels\/package\?lots=4%3A2/);
 const frozen=page('?finished_stock=1&job_id=12');await frozen.api.loadPackage();assert.equal(frozen.calls[0].url,'/api/production/finished-product-labels/jobs/12');
});
test('reprint cancellation writes nothing, confirmed reprint once; uncertain result reuses exact request',async()=>{
 const h=page();h.api.set(pkg,true);let confirms=0;h.window.confirm=()=>{confirms++;return false;};await assert.rejects(h.api.preparePrintJob(),/已取消补打/);assert.equal(h.calls.length,0);
 h.window.confirm=()=>{confirms++;return true;};h.request(async()=>{throw new TypeError('lost response');});await assert.rejects(h.api.preparePrintJob());const first=JSON.parse(h.calls[0].opt.body);assert.equal(first.reprint_confirmed,true);
 h.request(async()=>({ok:true,json:async()=>({job_id:12,job:{id:12,status:'prepared'},package:pkg})}));await h.api.preparePrintJob();assert.deepEqual(JSON.parse(h.calls[1].opt.body),first);assert.equal(confirms,2);
});

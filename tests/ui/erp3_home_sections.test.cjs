const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const read=file=>process.env.ERP3_FRONTEND_BASELINE?require('node:child_process').execFileSync('git',['show',`${process.env.ERP3_FRONTEND_BASELINE}:${file}`],{encoding:'utf8',maxBuffer:8*1024*1024}):fs.readFileSync(file,'utf8');
const html=read('static/index.html'),vue={console};vm.runInNewContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),vue);const Vue=vue.Vue;
function fixture(groups=['stock','production','delivery','finance','approval']){
 const pending=[],controllers=new Map(),calls=[],window={};
 window.axios={get:(url,config)=>new Promise((resolve,reject)=>pending.push({url,config,resolve,reject}))};
 vm.runInNewContext(fs.readFileSync('static/js/list-read-state.js','utf8'),{window});
 const exported={exports:{}};vm.runInNewContext(read('static/ui/home-workbench.js'),{window,module:exported,console});const {mixin,template}=exported.exports;
 const names=['beginLatestRequest','finishLatestRequest','cancelLatestRequest','isCancelledRequest','loadOverview','dashboardRequisitionHoldSummary'];
 const methods=names.map(n=>{const start=html.search(new RegExp(`          (?:async )?${n}\\(`));assert.ok(start>=0,n);return html.slice(start,html.indexOf('\n          },',start)+12);}).join(',');
 const actions=vm.runInNewContext(`({${methods}})`,{AbortController,window,axios:window.axios,latestRequestControllers:controllers});
 const raw={...mixin.data.call({}),activePage:'dashboard',user:{id:1},authGeneration:1,loading:false,pageLoading:false,listReadStates:{},
  overview:{mode:'summary',as_of:'2026-10-22T10:00:00',available_groups:groups,cards:[{key:'pending_material',count:null},{key:'pending_delivery',count:null},{key:'pending_payment',count:null}],workbench:{today:'2026-10-22',customers:[{id:1,label:'active'}],can_review:true,can_view_approvals:true,tasks:[]}},
  overviewError:'',overviewLoading:false,hasPermission:()=>true,errorMessage:e=>e.message,
  $nextTick:fn=>Promise.resolve().then(fn),loadKpi:async()=>calls.push('kpi'),loadWarehouseCapacitySummary:async()=>calls.push('capacity'),loadDeliveryBacklogs:async()=>calls.push('backlogs'),
  syncDesktopDeliveryMargin(){},...mixin.methods,...actions,homeMeasure(){}};
 let state;for(const [name,get]of Object.entries(mixin.computed))raw[name]=Vue.computed(()=>get.call(state));raw.dashboardCards=Vue.computed(()=>state.overview.cards || []);raw.dashboardRequisitionHoldSummary=Vue.computed(()=>actions.dashboardRequisitionHoldSummary.call(state));
 state=Vue.reactive(raw);
 const stops=['authGeneration','activePage'].map(name=>Vue.watch(()=>state[name],value=>mixin.watch[name].call(state,value)));
 return {state,pending,calls,controllers,mixin,template,stop:()=>stops.forEach(stop=>stop())};
}
const response=(group,tasks=[],extra={})=>({data:{group,as_of:'2026-10-22T10:01:00',cards:[],workbench:{tasks,customers:[]},...extra}});
const tick=()=>new Promise(setImmediate);
test('summary first paint never waits for heavy sections, capacity, delivery backlog or KPI',async()=>{
 const f=fixture(),s=f.state;s.overview={};const load=s.loadOverview();assert.equal(f.pending[0].config.params.mode,'summary');
 f.pending[0].resolve({data:{mode:'summary',as_of:'now',available_groups:['production'],cards:[],workbench:{today:'2026-10-22',tasks:[],customers:[]}}});
 assert.equal(await load,true);assert.equal(s.overviewLoading,false);assert.equal(f.pending.length,1);assert.deepEqual(f.calls,[]);assert.equal(s.loading,false);
 assert.match(f.template,/<product-workbench/);assert.match(f.template,/<home-collaboration/);assert.ok(f.template.indexOf('<home-collaboration')<f.template.indexOf('<template v-if="vm.overview.as_of'));
 f.stop();
});
test('only the requested authorized group loads; unread counts differ from a verified zero',async()=>{
 const f=fixture(['production','delivery']),s=f.state;assert.equal(s.homeLaunchers.find(x=>x.key==='production').count,null);
 s.homeDialog='production';const load=s.homeLoadDialog();assert.equal(f.pending.length,1);assert.equal(f.pending[0].config.params.group,'production');assert.equal(s.homeDialogReadState.loading,true);assert.equal(s.homeDialogReadState.loaded,false);assert.equal(s.loading,false);
 f.pending[0].resolve(response('production',[],{cards:[{key:'pending_material',count:0}]}));assert.equal(await load,true);assert.equal(s.homeDialogReadState.loaded,true);assert.equal(s.homeLaunchers.find(x=>x.key==='production').count,0);assert.equal(s.homeLaunchers.find(x=>x.key==='delivery').count,null);
 assert.equal(await s.homeLoadSection('finance'),false);assert.equal(await s.homeLoadDialog(),true);assert.equal(f.pending.length,1);f.stop();
});
test('independent groups merge with historical customers; approval appears once in the aggregate',async()=>{
 const f=fixture(['production','approval']),s=f.state;s.homeDialog='priority';const load=s.homeLoadDialog();assert.deepEqual(f.pending.map(x=>x.config.params.group),['production','approval']);
 f.pending[1].resolve(response('approval',[{id:'a',key:'approval',customer_id:9,action_text:'审批'}],{workbench:{tasks:[{id:'a',key:'approval',customer_id:9,action_text:'审批'}],customers:[{id:9,label:'historical'}]}}));await tick();assert.equal(s.homePriorityTotal,null);
 f.pending[0].resolve(response('production',[{id:'p',key:'pending_material',customer_id:1,due_date:'2026-10-22',urgency:'today'}]));await load;
 assert.equal(s.homeData.tasks.length,2);assert.equal(s.homeData.tasks.filter(x=>x.key==='approval').length,1);assert.equal(s.homeData.customers.map(x=>x.id).join(','),'1,9');assert.equal(s.homePriorityTotal,2);f.stop();
});
test('section refresh failure retains the prior facts and timestamp, with retry state',async()=>{
 const f=fixture(['production']),s=f.state;s.homeDialog='production';const first=s.homeLoadDialog();f.pending[0].resolve(response('production',[{id:'original',key:'pending_material'}]));await first;const previous=s.homeSections.production;
 const refresh=s.homeLoadDialog('production',{force:true});assert.equal(s.homeSections.production,previous);f.pending[1].reject(new Error('offline'));assert.equal(await refresh,false);
 assert.equal(s.homeSections.production,previous);assert.equal(s.homeDialogReadState.loaded,true);assert.equal(s.homeDialogReadState.loading,false);assert.equal(s.homeDialogReadState.error,'offline');assert.equal(s.homeData.tasks[0].id,'original');
 const retry=s.homeLoadDialog('production',{force:true});f.pending[2].resolve(response('production'));assert.equal(await retry,true);assert.equal(s.homeDialogReadState.error,'');assert.equal(s.homeData.tasks.length,0);f.stop();
});
test('out-of-order group refresh cannot repaint or release the current indicator',async()=>{
 const f=fixture(['production']),s=f.state;s.homeDialog='production';const old=s.homeLoadDialog(),newer=s.homeLoadDialog('production',{force:true});assert.equal(f.pending[0].config.signal.aborted,true);
 f.pending[0].resolve(response('production',[{id:'old'}]));await old;assert.equal(s.homeDialogReadState.loading,true);assert.equal(s.homeSections.production,undefined);
 f.pending[1].resolve(response('production',[{id:'new'}]));await newer;assert.equal(s.homeData.tasks[0].id,'new');assert.equal(s.homeDialogReadState.loading,false);f.stop();
});
test('leaving home or changing actor rejects in-flight data and clears the owned indicator',async()=>{
 for(const change of [s=>s.activePage='orders',s=>{s.user={id:2};s.authGeneration++;}]){
  const f=fixture(['production']),s=f.state;s.homeDialog='production';const load=s.homeLoadDialog();change(s);await Vue.nextTick();assert.equal(f.pending[0].config.signal.aborted,true);
  f.pending[0].resolve(response('production',[{id:'foreign'}]));assert.equal(await load,false);assert.equal(s.homeSections.production,undefined);assert.equal(s.listReadStates['dashboard:section:production'].loading,false);f.stop();
 }
});
test('overview cancellation after navigation releases its indicator without replacing its snapshot',async()=>{
 const f=fixture(),s=f.state,previous=s.overview,load=s.loadOverview();s.activePage='orders';f.pending[0].resolve({data:{mode:'summary',as_of:'obsolete'}});assert.equal(await load,false);assert.equal(s.overview,previous);assert.equal(s.overviewLoading,false);f.stop();
});
test('a business refresh away from home invalidates its cached group and return resumes the same dialog',async()=>{
 const f=fixture(['production']),s=f.state;s.homeDialog='production';const first=s.homeLoadDialog();f.pending[0].resolve(response('production',[{id:'old'}]));await first;
 s.activePage='orders';await Vue.nextTick();const overview=s.loadOverview();f.pending[1].resolve({data:{...s.overview,as_of:'after-save'}});await overview;
 assert.equal(s.homeSectionLoadedAt.production,0);assert.equal(f.pending.length,2);s.activePage='dashboard';await Vue.nextTick();assert.equal(s.homeDialog,'production');assert.equal(f.pending[2].config.params.group,'production');
 f.pending[2].resolve(response('production',[{id:'new'}]));await tick();assert.equal(s.homeData.tasks[0].id,'new');f.stop();
});
test('permission changes discard previously loaded sections and historical customers',async()=>{
 const f=fixture(['production','approval']),s=f.state;s.homeDialog='approval';const first=s.homeLoadDialog();f.pending[0].resolve(response('approval',[],{workbench:{tasks:[{id:'private',key:'approval'}],customers:[{id:9,label:'private'}]}}));await first;
 const load=s.loadOverview();f.pending[1].resolve({data:{...s.overview,available_groups:['production']}});await load;assert.equal(s.homeSections.approval,undefined);assert.equal(s.homeData.tasks.length,0);assert.equal(s.homeData.customers.length,1);f.stop();
});
test('production details restore waiting requisition entry and due count without turning unread into zero',async()=>{
 const f=fixture(['production']),s=f.state;assert.equal(s.dashboardRequisitionHoldSummary.total,null);
 const load=s.homeLoadDialog('production');f.pending[0].resolve(response('production',[],{cards:[{key:'pending_material',count:2,waiting_count:4,due_waiting_count:1}]}));await load;
 assert.equal(s.dashboardRequisitionHoldSummary.total,4);assert.equal(s.dashboardRequisitionHoldSummary.due_count,1);assert.match(f.template,/v-if="vm.dashboardRequisitionHoldSummary.total"/);f.stop();
});

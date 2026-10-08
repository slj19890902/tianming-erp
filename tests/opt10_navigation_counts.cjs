const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
const source=fs.readFileSync(path.join(process.argv[2]||path.join(__dirname,'..'),'static/index.html'),'utf8');
const start=source.indexOf('menus() {');const end=source.indexOf('deliveryCustomers() {',start);
const roleMenus={admin:['dashboard','production','warehouse','deliveries','finance','orders','customers','system']};
const menus=eval('('+source.slice(start,end).trim().replace(/,$/,'').replace(/^menus\(/,'function menus(')+')');
function state(cards=[]){return {
  user:{id:1,role:'admin'},overview:{cards},overviewLoading:false,overviewError:'',
  ordersUnfinishedTotal:77,requisitionPendingOverallTotal:88,incomingPendingTotal:99,productionPlacementPendingTotal:0,deliveriesTotal:567,
  navigationGroups:[{key:'workbench',pages:[{key:'orders'}]},{key:'master',pages:[{key:'customers'}]},{key:'system_hub',pages:[{key:'system'}]}],
  pagePermission:()=>true,pageAllowed:()=>true,warehouseNavigationMenus:rows=>rows,
};}
const cold=menus.call(state());
assert.equal(cold.find(x=>x.key==='requisition').count,undefined,'an unloaded count must not claim zero work');
const cards=[{key:'pending_material',title:'待报料',count:4,count_unit:'项'}, {key:'pending_production',title:'待生产',count:3,count_unit:'任务'}, {key:'pending_delivery',title:'待送货',count:2,count_unit:'客户'}];
const vm=state(cards);const loaded=menus.call(vm);
assert.equal(loaded.find(x=>x.key==='workbench').count,4,'do not add overlapping orders and material stages');
assert.equal(loaded.find(x=>x.key==='requisition').count,4,'the material entry uses the material workload');
assert.ok(!loaded.some(x=>x.key==='production'),'production remains in the process navigation');
assert.equal(loaded.find(x=>x.key==='deliveries').count,2,'use customer workload rather than filtered/history delivery count');
vm.deliveriesTotal=0;assert.equal(menus.call(vm).find(x=>x.key==='deliveries').count,2);
assert.equal(loaded.find(x=>x.key==='deliveries').countUnit,'客户');
const zero=state([{key:'pending_material',count:0,count_unit:'项'}]);
assert.equal(menus.call(zero).find(x=>x.key==='requisition').count,0,'a measured zero remains visible');
for(const invalid of [null,undefined,-1,NaN,'bad']) {
 const bad=state([{key:'pending_material',count:invalid,count_unit:'项'}]);
 assert.equal(menus.call(bad).find(x=>x.key==='requisition').count,undefined);
}
const failure=state(cards);failure.overviewError='unavailable';
assert.equal(menus.call(failure).find(x=>x.key==='requisition').count,undefined);
const loading=state(cards);loading.overviewLoading=true;
assert.equal(menus.call(loading).find(x=>x.key==='requisition').count,undefined);
const noUnit=state([{key:'pending_material',count:3}]);
assert.equal(menus.call(noUnit).find(x=>x.key==='requisition').count,undefined,'unknown units must not be presented as a known workload');
assert.match(loaded.find(x=>x.key==='requisition').countDetails,/首页.*刷新/);
const denied=state(cards);denied.pageAllowed=key=>key!=='production';
assert.ok(!menus.call(denied).some(x=>x.key==='production'));
const worker=state(cards);worker.pageAllowed=key=>key==='production';
const workerMenus=menus.call(worker);assert.equal(workerMenus.find(x=>x.key==='production').count,3);assert.ok(!workerMenus.some(x=>x.key==='requisition'));
console.log('navigation unknown state, authoritative units and independence from page filters passed');

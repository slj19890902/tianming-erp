const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {mixin,template,dialogComponent:dialog}=require('../../static/ui/home-workbench.js');
const {component:collab}=require('../../static/ui/home-collaboration.js');
test('real shipped Vue compiles the workbench, dialog, and collaboration templates',()=>{
 const ctx={console};vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),ctx);
 for(const source of [template,dialog.template,collab.template]){const errors=[];assert.equal(typeof ctx.Vue.compile(source,{decodeEntities:s=>s,onError:e=>errors.push(e)}),'function');assert.deepEqual(errors,[]);}
});
test('global mixin does not evaluate root permission data on child dialogs',()=>{
 assert.deepEqual(mixin.computed.homeLaunchers.call({$parent:{}}),[]);
});
test('unauthorized dialog does not open; allowed opening and closing preserve query and customer',()=>{
 const c={homeDialog:'',homeCustomer:7,homeQuery:'PO42',homeStockPage:3,homeLaunchers:[{key:'stock'}],homeLoadDialog:mixin.methods.homeLoadDialog,$nextTick:fn=>fn(),homeMeasure(){}};
 mixin.methods.homeOpenDialog.call(c,'finance');assert.equal(c.homeDialog,'');
 mixin.methods.homeOpenDialog.call(c,'stock');assert.equal(c.homeDialog,'stock');assert.equal(c.homeWorkspace,'stock');
 c.homePreferenceBusy=true;mixin.methods.homeCloseDialog.call(c);assert.equal(c.homeDialog,'stock');
 c.homePreferenceBusy=false;mixin.methods.homeCloseDialog.call(c);assert.equal(c.homeDialog,'');assert.equal(c.homeCustomer,7);assert.equal(c.homeQuery,'PO42');assert.equal(c.homeStockPage,3);
});
test('nested business dialogs and product views take priority without losing the home dialog',()=>{
 for(const root of [{activePage:'orders'},{activePage:'dashboard',modal:{type:'stockPolicy'}},{activePage:'dashboard',productWorkbenchOpen:true}])assert.equal(dialog.computed.active.call({$root:root}),false);
 assert.equal(dialog.computed.active.call({$root:{activePage:'dashboard',modal:null}}),true);
});
test('workflow tabs use permission-filtered stages and never mix finance with production',()=>{
 const c={homeWorkspace:'finance',homeStages:[{key:'pending_material'},{key:'pending_invoice'},{key:'pending_payment'}]};
 assert.deepEqual(mixin.computed.homeWorkflowFilters.call(c).map(x=>x.key),['pending_invoice','pending_payment']);
});
test('compact viewport preserves edited memo and upload, full list is bounded and collapses to summary',()=>{
 const c={editId:'m1',editText:'未保存文字',pendingUpload:{key:'same-upload'},memoAll:false,filesAll:false,summaryRows:1};
 collab.watch.compact.call(c,true);assert.equal(c.memoAll,true);assert.equal(c.editText,'未保存文字');assert.equal(c.pendingUpload.key,'same-upload');
 assert.equal(collab.computed.memoLimit.call(c),20);assert.equal(collab.computed.fileLimit.call(c),1);
 c.memoAll=false;assert.equal(collab.computed.memoLimit.call(c),1);
});
test('closing memo expansion keeps the new draft; closing file expansion preserves pending upload',()=>{
 const c={memoAll:true,completed:true,draft:'原草稿',filesAll:true,pendingUpload:{key:'unchanged'},loadMemos(){},loadFiles(){}};
 collab.methods.showMemos.call(c);assert.equal(c.memoAll,false);assert.equal(c.completed,false);assert.equal(c.draft,'原草稿');
 collab.methods.showFiles.call(c);assert.equal(c.filesAll,false);assert.equal(c.pendingUpload.key,'unchanged');
});

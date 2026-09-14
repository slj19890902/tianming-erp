const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
let mixin,frames=new Map(),id=0,top=250,rowHeight=70,detailOpen=false;
const table={querySelectorAll:()=>[{cells:[1,2],getBoundingClientRect:()=>({height:rowHeight})}],getBoundingClientRect:()=>({top}),tHead:{offsetHeight:32}};
const card={getBoundingClientRect:()=>({height:500}),querySelector:s=>s.startsWith('.order-item-sub-row')?(detailOpen?{}:null):s.includes('table')?table:{offsetHeight:50}};
const sandbox={innerWidth:1400,innerHeight:900,document:{querySelectorAll:()=>[card]},requestAnimationFrame:fn=>{frames.set(++id,fn);return id;},cancelAnimationFrame:n=>frames.delete(n)};
vm.runInNewContext(fs.readFileSync('static/ui/pdf-workspace.js','utf8'),sandbox);
sandbox.ERPPdfWorkspace.install({mixin:m=>mixin=m});
const state={modal:{type:'orderPdfImport'},pdfFitCapacity:0,pdfActiveDraftKey:'a',...mixin.methods};
function measure(){mixin.updated.call(state);for(let i=0;i<2;i++){const batch=[...frames.values()];frames.clear();batch.forEach(fn=>fn());}}
measure();assert.equal(state.pdfFitCapacity,7);
sandbox.innerHeight=450;measure();assert.equal(state.pdfFitCapacity,1);
sandbox.innerHeight=900;measure();assert.equal(state.pdfFitCapacity,7);
top=800;measure();assert.equal(state.pdfFitCapacity,1);
top=250;measure();assert.equal(state.pdfFitCapacity,7,'stable layout recovers even without a second resize');
rowHeight=140;measure();assert.equal(state.pdfFitCapacity,3);
rowHeight=70;measure();assert.equal(state.pdfFitCapacity,3,'shorter page must not oscillate back to overflowing taller rows');
state.modal=null;mixin.watch['modal.type'].call(state);assert.equal(state.pdfFitCapacity,0);
state.modal={type:'orderPdfImport'};mixin.watch['modal.type'].call(state);measure();assert.equal(state.pdfFitCapacity,7);
const draft={items:Array.from({length:20},(_,id)=>({id,quantity:2})),_product_page:2};
const before=JSON.stringify(draft);assert.equal(state.pdfPageItems(draft).length,7);assert.equal(JSON.stringify(draft),before);
detailOpen=true;top=800;measure();assert.equal(state.pdfFitCapacity,7,'opening details must not repage');
top=100;measure();assert.equal(state.pdfFitCapacity,7,'scrolling details must not oscillate');
assert.equal(state.pdfPageItems(draft)[0].id,7,'second-page identity stays stable');
sandbox.innerHeight=450;measure();assert.equal(state.pdfFitCapacity,3,'real resize is still measured');
detailOpen=false;
state.$nextTick=()=>{};
for(const kind of ['semi','raw']){
 const item={_inventory:{finished:{candidates:[]},semi:{main:{manual_candidates:[{lot_id:1,material_kind:kind}]}}}};
 const original=JSON.stringify(item._inventory);
 state.pdfOpenInventory(item);assert.equal(item._inventory_tab,kind);
 assert.equal(JSON.stringify(item._inventory),original,'opening inventory must not select or mutate allocations');
}
mixin.updated.call(state);mixin.beforeUnmount.call(state);assert.equal(frames.size,0);
console.log('PDF resize, recovery, reopen, stable paging and draft preservation passed');

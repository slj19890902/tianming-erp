const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=path.resolve(__dirname,'../..');
const ui=require('../../static/ui/demand-stock.js');
test('stock request includes zero and repeats; latest response wins',async()=>{
  const waiting=[];global.axios={post:(url,body)=>new Promise(resolve=>waiting.push({body,resolve}))};
  const c={...ui.summary.data(),customerId:1,lines:[{product_id:1,quantity:30}]};
  const a=ui.summary.methods.refresh.call(c);c.lines=[{product_id:1,quantity:50}];
  const b=ui.summary.methods.refresh.call(c);
  waiting[1].resolve({data:{items:[{quantity:50}]}});await b;
  waiting[0].resolve({data:{items:[{quantity:30}]}});await a;
  assert.equal(c.rows[0].quantity,50);
  assert.equal(ui.normalized([{product_id:1,quantity:0},{product_id:1,quantity:3}]).length,2);
});
test('common-box projection mirrors quantity replacement, retains repeated existing lines',()=>{
  const c={orderForm:{items:[{product_id:1,quantity:10},{product_id:1,quantity:15}]},
    orderCommonBoxPicker:{selected:{1:{product:{id:1},quantity:20}},items:[{id:1},{id:2}]}};
  assert.deepEqual(ui.mixin.methods.commonBoxDemandLines.call(c),[{product_id:1,quantity:20},{product_id:1,quantity:15},{product_id:2,quantity:0}]);
});
test('new Vue templates compile with bundled production Vue',()=>{
  const context={console};vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root,'static/vendor/vue-3.5.40.global.prod.js'),'utf8'),context);
  for(const c of [ui.summary,ui.importer]) assert.equal(typeof context.Vue.compile(c.template,{decodeEntities:value=>value,onError:error=>{throw error;}}),'function');
  const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
  const template=html.slice(html.indexOf('<div id="app"'),html.lastIndexOf('<script>'));
  assert.equal(typeof context.Vue.compile(template,{decodeEntities:value=>value.replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>'),onError:error=>{throw error;}}),'function');
});
test('main and print inline JavaScript parse',()=>{
  for(const name of ['index.html','requisition-production-print.html']) {
    const html=fs.readFileSync(path.join(root,'static',name),'utf8');
    for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) if(match[1].trim()) new vm.Script(match[1]);
  }
});

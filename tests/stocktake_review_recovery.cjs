const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const recovery=require('../static/ui/stocktake-review-recovery.js');
let passed=0;
class Store{
  constructor(){this.map=new Map();this.failGet=false;this.failSet=false;this.failRemove=false}
  get length(){return this.map.size}key(index){return [...this.map.keys()][index]||null}
  getItem(key){if(this.failGet)throw Error('get failed');return this.map.get(key)??null}
  setItem(key,value){if(this.failSet)throw Error('set failed');this.map.set(key,value)}
  removeItem(key){if(this.failRemove)throw Error('remove failed');this.map.delete(key)}
}
const clone=value=>JSON.parse(JSON.stringify(value));
function receipt(row){
  const reason=recovery.reason(row.action,row.body),target=row.action==='approve'?'approved':'rejected';
  const item={id:1,inventory_lot_id:1,lot_version_snapshot:3,quantity_available_snapshot:10,quantity_reserved_snapshot:2,quantity_on_hand_snapshot:12,counted_quantity:15,difference_quantity:3,adjustment_movement_id:row.action==='approve'?1:null};
  const review={id:1,sequence:1,action:row.action,from_status:'submitted',to_status:target,reason,reviewed_by:row.ownerId,reviewed_by_name:null,reviewed_at:'2026-10-10T01:00:00Z',details:row.action==='approve'?{adjustments:[{inventory_lot_id:1,before_available:10,reserved:2,counted_quantity:15,after_available:13,delta:3,movement_id:1}]}:{order_id:row.orderId,order_number:'ST-test',idempotency_key:row.body.idempotency_key,reason}};
  return {id:row.orderId,order_number:'ST-test',version:3,location_id:1,lot_count:1,snapshot_on_hand:12,counted_quantity:15,difference_quantity:3,status:target,items:[item],reviews:[review],reviewed_by:row.ownerId,reviewed_at:review.reviewed_at,review_note:reason,request_action:row.action,request_idempotency_key:row.body.idempotency_key,request_reason:reason,current_actor_id:row.ownerId,matched_review:{...review,idempotency_key:row.body.idempotency_key}};
}
function found(row,order=receipt(row)){return {status:'found',request_action:row.action,request_idempotency_key:row.body.idempotency_key,request_reason:recovery.reason(row.action,row.body),current_actor_id:row.ownerId,observed_at:'2026-10-10T01:01:00Z',matched_review:order.matched_review,order}}
function context(handler){
  const storage=new Store(),calls=[];let actor=1,sequence=0;
  const manager=recovery.create({storage,actor:()=>actor,newKey:()=>`key-${++sequence}`,request:async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return handler?handler(url,options):receipt(manager.entries()[0])}});
  return {storage,calls,manager,setActor:value=>{actor=value}};
}
async function test(name,fn){await fn();passed++}
(async()=>{
  for(const action of ['approve','reject'])await test(`complete ${action}`,async()=>{
    const c=context(),row=c.manager.prepare(1,action);assert.equal((await c.manager.send(row)).status,'completed');assert.equal(c.storage.length,0);
  });
  for(const [name,mutate] of [
    ['empty',()=>({})],['partial',()=>({id:1})],['missing version',x=>{delete x.version;return x}],['missing location',x=>{delete x.location_id;return x}],
    ['lot count',x=>({...x,lot_count:0})],['system sum',x=>({...x,snapshot_on_hand:0})],['count sum',x=>({...x,counted_quantity:0})],['difference sum',x=>({...x,difference_quantity:0})],
    ['wrong key',x=>({...x,request_idempotency_key:'another'})],['wrong actor',x=>({...x,current_actor_id:2})],['no reviews',x=>({...x,reviews:[]})],['bad review',x=>({...x,matched_review:{...x.matched_review,to_status:'rejected'}})],['bad historical actor',x=>({...x,matched_review:{...x.matched_review,reviewed_by:2}})],
  ])await test(name,async()=>{
    const c=context(()=>mutate(receipt(row))),row=c.manager.prepare(1,'approve');assert.equal((await c.manager.send(row)).status,'unknown');assert.equal(c.storage.length,1);
  });
  await test('valid empty location',async()=>{
    const c=context(),row=c.manager.prepare(1,'approve'),order=receipt(row);order.items=[];order.lot_count=order.snapshot_on_hand=order.counted_quantity=order.difference_quantity=0;order.reviews[0].details.adjustments=[];order.matched_review.details.adjustments=[];assert(recovery.validReceipt(order,row));
  });
  for(const status of [401,403,409,422])await test(`unknown ${status}`,async()=>{
    const c=context(()=>{throw Error(`HTTP ${status}`)}),row=c.manager.prepare(1,'approve');await c.manager.send(row);assert.equal(c.manager.forOrder(1).length,1);
    assert.throws(()=>c.manager.prepare(1,'reject'));assert(c.manager.prepare(2,'reject'));
  });
  await test('canonical frozen body',async()=>{
    const c=context(),row=c.manager.prepare(1,'approve',{reason:'original'});
    for(const altered of [{...row,action:'reject'},{...row,body:{...row.body,reason:'changed'}}])assert.equal((await c.manager.send(altered)).status,'unknown');
    assert.equal(c.calls.length,0);await c.manager.send(row);assert.equal(c.calls[0].body.reason,'original');
  });
  await test('storage write before send',async()=>{const c=context();c.storage.failSet=true;assert.throws(()=>c.manager.prepare(1,'approve'));assert.equal(c.calls.length,0)});
  await test('storage read failure',async()=>{const c=context(),row=c.manager.prepare(1,'approve');c.storage.failGet=true;assert.equal((await c.manager.send(row)).status,'unknown');assert.equal(c.calls.length,0);assert.equal(c.storage.map.size,1)});
  await test('storage read failure after network',async()=>{const c=context(()=>{c.storage.failGet=true;throw Error('network')}),row=c.manager.prepare(1,'approve');assert.equal((await c.manager.send(row)).status,'unknown');assert.equal(c.storage.map.size,1)});
  for(const mode of ['write','remove'])await test(`confirmed storage ${mode}`,async()=>{
    const c=context(()=>{if(mode==='write')c.storage.failSet=true;else c.storage.failRemove=true;return receipt(row)}),row=c.manager.prepare(1,'approve');
    assert.equal((await c.manager.send(row)).status,'confirmed');assert.equal(c.manager.forOrder(1)[0].state,'confirmed');
    c.storage.failSet=c.storage.failRemove=false;assert.equal((await c.manager.resolve(c.manager.forOrder(1)[0])).status,'completed');assert.equal(c.calls.length,1);
  });
  await test('not found explicit same body continue',async()=>{
    const c=context(url=>url.endsWith('review-result')?{...found(row),status:'not_found',order:null,matched_review:null}:receipt(row)),row=c.manager.prepare(1,'reject');
    assert.equal((await c.manager.resolve(row)).status,'not_found');assert.equal(c.calls.length,1);assert.equal(c.storage.length,1);
    assert.equal((await c.manager.continue(row)).status,'completed');assert.deepEqual(c.calls[0].body.body,c.calls[1].body);
  });
  await test('resolve exact found',async()=>{const c=context(()=>found(row)),row=c.manager.prepare(1,'approve');assert.equal((await c.manager.resolve(row)).status,'completed')});
  await test('late actor switch',async()=>{
    let release;const c=context(()=>new Promise(resolve=>{release=resolve})),row=c.manager.prepare(1,'approve'),promise=c.manager.send(row);c.setActor(2);release(receipt(row));assert.equal((await promise).status,'other_actor');assert.equal(c.storage.length,1);assert.equal(c.manager.entries().length,0);c.setActor(1);assert.equal(c.manager.entries().length,1);
  });
  await test('double send',async()=>{
    let release;const c=context(()=>new Promise(resolve=>{release=resolve})),row=c.manager.prepare(1,'approve'),promise=c.manager.send(row);assert.equal((await c.manager.send(row)).status,'busy');assert.equal(c.calls.length,1);release(receipt(row));await promise;
  });
  await test('independent concurrent persistent keys',async()=>{
    const c=context(),row=c.manager.prepare(1,'approve'),second={...clone(row),body:{...row.body,idempotency_key:'other-tab-key'}};
    c.storage.setItem('tianming:stocktake-review:v1:1:1:other-tab-key',JSON.stringify(second));assert.equal(c.manager.forOrder(1).length,2);assert.throws(()=>c.manager.prepare(1,'reject'));assert(c.manager.prepare(2,'approve'));
  });
  await test('reload and corrupt other order',async()=>{
    const c=context(),row=c.manager.prepare(1,'approve');const reload=recovery.create({storage:c.storage,actor:()=>1,newKey:()=> 'reload',request:async()=>found(row)});assert.equal(reload.forOrder(1).length,1);
    c.storage.setItem('tianming:stocktake-review:v1:1:9:bad','{');assert(reload.prepare(2,'reject'));assert.equal((await reload.resolve(row)).status,'completed');
  });
  const html=fs.readFileSync(process.env.STOCKTAKE_REVIEW_HTML||require('node:path').join(__dirname,'../static/warehouse.html'),'utf8');
  function inlineFunction(name){
    const marker=new RegExp(`^    (?:async )?function ${name}\\(`,'m'),match=marker.exec(html);assert(match,name);
    const after=html.slice(match.index),firstLine=after.split(/\r?\n/)[0];if(firstLine.trim().endsWith('}'))return firstLine;
    const next=/\n    (?:async )?function |\n    window\.addEventListener/.exec(after);
    return next?after.slice(0,next.index):after;
  }
  async function desktop(scenario){
    const store=new Store(),notes=[],calls=[],elements=new Map();let confirmations=0,closed=0,release;
    const element=()=>({classList:{toggle(){},remove(){}},appendChild(){},append(){},innerHTML:'',textContent:''});
    const context={StocktakeReviewRecovery:recovery,window:{localStorage:store},document:{createElement:element},
      state:{user:{id:1},readOnly:scenario==='readonly',stocktakeReviews:[],stocktakeReviewDetail:{id:1,order_number:'ST-display',location_name:'原货位'}},
      $:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)},
      canReviewStocktakes:()=>!context.state.readOnly,canViewStocktakes:()=>true,stocktakeNumber:row=>row.order_number,stocktakeLocation:row=>row.location_name,
      createIdempotencyKey:()=> 'desktop-key',confirm:()=>{confirmations++;return scenario!=='cancel'},
      toast:(message,error)=>notes.push({message,error}),closeStocktakeReview:()=>{closed++;context.state.stocktakeReviewDetail=null},
      stocktakeRows:data=>data.items||[],renderStocktakeReviews:()=>{},stocktakeDrift:()=>false,apiErrorMessage:(body,status)=>`HTTP ${status}`,apiErrorCode:()=>null,
      fetch:async(url,options)=>{
        calls.push({url,method:options.method||'GET',body:options.body});
        if(options.method==='POST'){
          const orderId=Number(url.match(/stocktakes\/(\d+)/)[1]);const row=[...store.map.values()].map(value=>JSON.parse(value)).find(row=>row.orderId===orderId);
          if(scenario==='late-detail'||scenario==='late-actor')await new Promise(resolve=>{release=resolve});
          return {ok:true,status:200,json:async()=>scenario==='empty'?{}:receipt(row)};
        }
        return {ok:scenario!=='refresh-failed',status:scenario==='refresh-failed'?503:200,json:async()=>({items:[]})};
      }};
    vm.createContext(context);
    const names=['api','stocktakeReviewRecovery','renderStocktakeReviewRecovery','showStocktakeReviewOutcome','recoverStocktakeReview','startStocktakeReview','approveStocktake','rejectStocktake','loadStocktakeReviews'];
    vm.runInContext('let stocktakeReviewRecoveryInstance=null;const stocktakeReviewObservations=new Map();\n'+names.map(inlineFunction).join('\n'),context);
    if(scenario==='other-pending'){
      const original={version:1,state:'unknown',ownerId:1,orderId:1,action:'reject',body:{idempotency_key:'A-key',expected_actor_id:1},display:{orderNumber:'ST-A',locationName:'A货位'}};
      store.setItem('tianming:stocktake-review:v1:1:1:A-key',JSON.stringify(original));context.state.stocktakeReviewDetail={id:2,order_number:'ST-B',location_name:'B货位'};
    }
    const promise=context.approveStocktake(scenario==='other-pending'?2:1);
    if(scenario==='late-detail'){context.state.stocktakeReviewDetail={id:2};release()}
    if(scenario==='late-actor'){context.state.user={id:2};release()}
    await promise;
    if(['cancel','readonly'].includes(scenario)){assert.equal(store.length,0);assert.equal(calls.length,0)}
    else if(['empty','late-actor'].includes(scenario)){assert.equal(store.length,1);assert.equal(closed,0)}
    else if(scenario==='other-pending'){assert.equal(store.length,1);assert(JSON.parse([...store.map.values()][0]).display.orderNumber==='ST-A');assert(notes.some(row=>row.message.includes('ST-B · B货位：盘点审核通过')))}
    else {assert.equal(store.length,0);if(scenario==='late-detail')assert.equal(context.state.stocktakeReviewDetail.id,2);else assert.equal(closed,1)}
    if(scenario==='refresh-failed'){assert(notes.some(row=>row.message.includes('ST-display · 原货位：盘点审核通过')));assert(notes.some(row=>row.message.includes('列表加载失败')))}
    assert.equal(confirmations,scenario==='readonly'?0:1);
  }
  for(const scenario of ['cancel','readonly','empty','refresh-failed','late-detail','late-actor','other-pending'])await test(`actual desktop ${scenario}`,()=>desktop(scenario));
  async function detailRace(scenario){
    let selected=null,rendered=null,closed=0,notes=[];const requests=new Map();
    const context={state:{user:{id:1},stocktakeReviewSelectedId:null,stocktakeReviewDetail:null},
      canViewStocktakes:()=>true,selectStocktakeReview:id=>{context.state.stocktakeReviewSelectedId=Number(id)},
      ensureStocktakeReviewModal:()=>{},$:()=>({innerHTML:'',classList:{remove(){},add(){closed++}}}),
      renderStocktakeDetail:data=>{rendered=data.id;context.state.stocktakeReviewDetail=data},
      toast:message=>notes.push(message),stocktakeDrift:()=>false,
      api:url=>new Promise((resolve,reject)=>requests.set(Number(url.match(/\/(\d+)$/)[1]),{resolve,reject}))};
    vm.createContext(context);
    vm.runInContext('let stocktakeReviewDetailGeneration=0;\n'+['openStocktakeReview','closeStocktakeReview'].map(inlineFunction).join('\n'),context);
    const first=context.openStocktakeReview(1);
    if(scenario==='closed')context.closeStocktakeReview();
    else if(scenario==='actor')context.state.user={id:2};
    else {const second=context.openStocktakeReview(2);requests.get(2).resolve({id:2});await second;assert.equal(rendered,2)}
    if(scenario==='late-failure')requests.get(1).reject(Error('late A error'));else requests.get(1).resolve({id:1});
    await first;
    if(['closed','actor'].includes(scenario))assert.equal(rendered,null);else assert.equal(rendered,2);
    assert.equal(notes.length,0);assert.equal(closed,scenario==='closed'?1:0);
  }
  for(const scenario of ['switched','closed','actor','late-failure'])await test(`actual detail generation ${scenario}`,()=>detailRace(scenario));
  const fixtureDir=process.env.STOCKTAKE_REVIEW_EVIDENCE;
  if(fixtureDir){
    for(const file of fs.readdirSync(fixtureDir).filter(file=>file.endsWith('-receipt.json'))){const data=JSON.parse(fs.readFileSync(`${fixtureDir}/${file}`,'utf8'));const row={version:1,state:'unknown',ownerId:data.ownerId,orderId:data.receipt.id,action:data.action,body:data.body};assert(recovery.validReceipt(data.receipt,row),file);passed++}
  }
  process.stdout.write(`${passed} stocktake review recovery cases passed\n`);
})().catch(error=>{process.stderr.write(error.stack);process.exit(1)});

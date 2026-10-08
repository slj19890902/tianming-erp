'use strict';
// Transport and dispatch unit tests only; no backend or rendered-browser claim.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict'),test=require('node:test'),crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/frontend-shell-bridge.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
  const listeners={},messages=[],calls=[];
  const parent={postMessage:value=>messages.push(value)};
  const window={parent,erpCheckingSession:false,addEventListener:(type,fn)=>listeners[type]=fn,setInterval:()=>1};
  window.removeEventListener=(type,fn)=>{if(listeners[type]===fn)delete listeners[type]};
  const responses=[];
  window.axios={interceptors:{response:{use:fn=>{responses.push(fn);return 1},eject:()=>calls.push('eject')}}};
  const document={documentElement:{classList:{add(){},remove(){},toggle(){}}},createElement:()=>({}),head:{appendChild(){}}};
  vm.runInNewContext(source,{window,document,location:{origin:'http://127.0.0.1:18381',search:'?frontend_shell=1'},URLSearchParams,clearInterval(){}});
  const instance={user:{id:1,role:'admin',must_change_password:false},authGeneration:1,loading:false,modal:{},canCreateOrders:true,hasPermission:()=>true,menus:[{key:'workbench',label:'订单主链'}],activePage:'orders',isMenuActive:()=>true,goMenu:key=>calls.push(key),$watch:()=>()=>{}};
  for(const [name,type] of [['openOrder','order'],['openOrderPdfImport','orderPdfImport'],['openEmailQueue','orderPdfImport']]) instance[name]=()=>{calls.push(name);instance.modal={type};};
  window.ERPFrontendShell.install(instance);
  const receive=(data,origin='http://127.0.0.1:18381',sender=parent)=>listeners.message({data,origin,source:sender});
  const entry=(action='import',requestId=crypto.randomUUID())=>({type:'tianming-formal-order-entry-v1',action,requestId});
  const replies=()=>messages.filter(m=>m.type==='tianming-formal-order-entry-result-v1');
  const handshake=()=>receive({type:'tianming-formal-shell-v1'});
  return {receive,instance,calls,messages,parent,window,entry,replies,handshake,responses,listeners};
}
test('only three original openers dispatch and acknowledged results contain no business data',async()=>{
  for(const [action,method] of [['new','openOrder'],['import','openOrderPdfImport'],['email','openEmailQueue']]){
    const h=setup();h.handshake();h.receive(h.entry(action));await tick();
    assert.deepEqual(h.calls,[method]);assert.equal(h.replies()[0].status,'opened');
    assert.deepEqual(Object.keys(h.replies()[0]).sort(),['action','requestId','status','type']);
  }
});
test('rejects foreign sender and origin, malformed id and arbitrary save/sync names',async()=>{
  const h=setup();h.handshake();
  h.receive(h.entry(),'https://foreign.invalid');h.receive(h.entry(),undefined,{});
  h.receive(h.entry('save'));h.receive(h.entry('sync'));h.receive(h.entry('import','bad'));
  await tick();assert.equal(h.calls.length,0);assert.equal(h.replies().length,0);
});
test('session, original page, permission and role boundaries deny entries',async()=>{
  for(const state of ['unconnected','anonymous','forced','checking','other-page','create-denied','view-denied','sales-email']){
    const h=setup();if(state!=='unconnected')h.handshake();
    if(state==='anonymous')h.instance.user=null;
    if(state==='forced')h.instance.user.must_change_password=true;
    if(state==='checking')h.window.erpCheckingSession=true;
    if(state==='other-page'){h.instance.activePage='warehouse';h.instance.go=()=>{};}
    if(state==='create-denied')h.instance.canCreateOrders=false;
    if(state==='view-denied')h.instance.hasPermission=()=>false;
    if(state==='sales-email')h.instance.user.role='sales';
    h.receive(h.entry('email'));await tick();assert.equal(h.calls.length,0,state);assert.equal(h.replies()[0].status,'denied',state);
  }
});
test('cached non-order workspace enters orders through original guarded navigation',async()=>{
  const h=setup();h.handshake();h.instance.activePage='requisition';
  h.instance.go=async page=>{h.calls.push('go:'+page);h.instance.activePage=page;};
  h.receive(h.entry('import'));await tick();
  assert.deepEqual(h.calls,['go:orders','openOrderPdfImport']);assert.equal(h.replies()[0].status,'opened');
});
test('navigation projects only a draft-open flag so closing a tab can protect unfinished input',async()=>{
  const h=setup();h.handshake();h.receive(h.entry('new'));await tick();
  const navigation=h.messages.filter(m=>m.type==='tianming-formal-navigation-v1').at(-1);
  assert.equal(navigation.draftOpen,true);assert.equal('orderForm' in navigation,false);
});
test('existing form and loading request are never reset by new entry',async()=>{
  for(const state of ['modal','loading']){
    const h=setup();h.handshake();const draft={type:'order',sentinel:'unchanged'};
    if(state==='modal')h.instance.modal=draft;else h.instance.loading=true;
    h.receive(h.entry('import'));await tick();assert.equal(h.calls.length,0);assert.equal(h.replies()[0].status,'busy');
    if(state==='modal')assert.equal(h.instance.modal,draft);
  }
});
test('in-flight and finished request replays do not open again; changed action is denied',async()=>{
  const h=setup();h.handshake();let release;
  h.instance.openOrderPdfImport=()=>{h.calls.push('openOrderPdfImport');return new Promise(resolve=>{release=()=>{h.instance.modal={type:'orderPdfImport'};resolve();};});};
  const request=h.entry();h.receive(request);h.receive(request);assert.equal(h.calls.length,1);
  release();await tick();h.instance.modal={};h.receive(request);await tick();
  assert.equal(h.calls.length,1);assert.equal(h.replies().at(-1).status,'opened');
  h.receive({...request,action:'new'});assert.equal(h.replies().at(-1).status,'denied');assert.equal(h.calls.length,1);
});
test('late completion after auth generation changes is not acknowledged as success',async()=>{
  const h=setup();h.handshake();h.instance.openOrder=()=>{h.instance.authGeneration++;h.instance.modal={type:'order'};};
  h.receive(h.entry('new'));await tick();assert.equal(h.replies()[0].status,'denied');
});
test('original method failure has a fixed safe status and no error leakage',async()=>{
  const h=setup();h.handshake();h.instance.openOrder=()=>{throw new Error('private exception must not cross bridge');};
  h.receive(h.entry('new'));await tick();assert.equal(h.replies()[0].status,'failed');assert.equal(JSON.stringify(h.messages).includes('private exception'),false);
});

test('existing save notifications carry only actor identity and cannot invoke mutations',()=>{
 const h=setup();h.handshake();const response={status:201,config:{method:'post',url:'/api/orders'},data:{private:'never-send'}};
 assert.equal(h.responses[0](response),response);
 const signal=h.messages.filter(m=>m.type==='tianming-formal-orders-changed-v1');assert.equal(signal.length,1);assert.deepEqual(Object.keys(signal[0]).sort(),['actorId','type']);
 assert.equal(signal[0].actorId,1);assert.equal(h.calls.length,0);
 for(const r of [{...response,status:409},{...response,config:{method:'get',url:'/api/orders'}},{...response,config:{method:'post',url:'/api/orders/inventory-draft-preview'}},{...response,config:{method:'post',url:'/api/mailbox/sync'}}])h.responses[0](r);
 assert.equal(h.messages.filter(m=>m.type==='tianming-formal-orders-changed-v1').length,1);
 h.instance.user=null;h.responses[0](response);assert.equal(h.messages.filter(m=>m.type==='tianming-formal-orders-changed-v1').length,1);
 h.listeners.pagehide();assert.equal(h.calls.at(-1),'eject');
});

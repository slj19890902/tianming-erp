const assert=require('node:assert/strict'),{test}=require('node:test'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const context={window:{}};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/assets/mold-print-selection.js'),'utf8'),context);const api=context.window.TmMoldPrintSelection;
const row=(id,printed=false,printable=true)=>({id,printable,label_print_status:{printed},location_guide:{cell_id:'a3',level:1,grid:3},label_name:String(id)});
const plain=value=>JSON.parse(JSON.stringify(value));
function fixture(){const calls=[],events=[],urls=[],remembered=[];let failure=null,allow=true,leaveFailure=false;
 const submission=api.createSubmission({request:async(url,payload)=>{calls.push(plain(payload));if(failure){let e=failure;failure=null;throw e}return{print_job_id:42}},navigate:url=>{if(leaveFailure){leaveFailure=false;throw new Error('navigation')}urls.push(url)},confirmReprint:()=>allow,changed:s=>events.push(plain(s)),makeKey:()=> 'same-key',remember:a=>remembered.push(plain(a))});
 return{submission,calls,events,urls,remembered,fail:e=>failure=e,cancel:()=>allow=false,failNavigation:()=>leaveFailure=true};}
test('A3 three unprinted default selected; previously printed stays manually selectable',()=>{
 const rows=[row(209),row(211),row(256),row(213,true),row(500,false,false)];
 assert.deepEqual(plain(api.defaultSelection(rows)),[209,211,256]);
 assert.throws(()=>api.defaultSelection([{id:1,printable:true}]),/状态/);
});
test('scope validates stable cell and rack, deduplicates and keeps invalid rows visible',()=>{
 const response={floor_code:'1F',rack:{rack_id:'a',cells:[{id:'a3'}]},items:[row(2),row(1),row(1),row(3,false,false)],total:4,truncated:false};
 assert.deepEqual(plain(api.scopeRows(response,{floorCode:'1F',rackId:'a',cellId:'a3'})).map(r=>r.id),[1,2,3]);
 assert.throws(()=>api.scopeRows({...response,truncated:true},{floorCode:'1F',rackId:'a'}),/完整/);
 assert.throws(()=>api.scopeRows(response,{floorCode:'1F',rackId:'a',cellId:'removed'}),/已变化/);
});
test('manual reprint accepted; cancel, zero, invalid and >100 selections make no writes',async()=>{
 const f=fixture();f.cancel();await f.submission.submit([row(1,true)],[1],'mold_80x40_v1');assert.equal(f.calls.length,0);
 for(const [rows,ids] of [[[row(1)],[]],[[row(1,false,false)],[1]],[Array.from({length:101},(_,i)=>row(i+1)),Array.from({length:101},(_,i)=>i+1)]]){await f.submission.submit(rows,ids,'mold_80x40_v1');assert.equal(f.calls.length,0)}
 const g=fixture();await g.submission.submit([row(1,true),row(2)],[1],'mold_80x40_v1');assert.deepEqual(g.calls[0].mold_ids,[1]);assert.match(g.urls[0],/mold_ids=1&.*print_job_id=42/);
});
test('uncertain response locks selection and replays the exact key and ids',async()=>{
 const f=fixture();f.fail(new Error('network'));await f.submission.submit([row(1),row(2)],[1],'mold_80x40_v1');assert.equal(f.submission.state().pending,true);
 await f.submission.submit([row(1),row(2)],[2],'mold_40x30_v1');assert.deepEqual(f.calls[0],f.calls[1]);assert.equal(f.submission.state().pending,false);
});
test('400-level error stays visible, restores choice and never navigates',async()=>{
 const f=fixture();f.fail(Object.assign(new Error('模具资料已变化'),{status:409}));await f.submission.submit([row(1)],[1],'mold_80x40_v1');
 assert.equal(f.submission.state().pending,false);assert.equal(f.urls.length,0);assert(f.events.some(s=>s.message==='模具资料已变化'));
});
test('double click only registers once; interrupted navigation reopens the committed job',async()=>{
 const f=fixture();await Promise.all([f.submission.submit([row(1)],[1],'mold_80x40_v1'),f.submission.submit([row(1)],[1],'mold_80x40_v1')]);assert.equal(f.calls.length,1);
 const g=fixture();g.failNavigation();await g.submission.submit([row(1)],[1],'mold_80x40_v1');await g.submission.submit([],[],'mold_80x40_v1');assert.equal(g.calls.length,1);assert.match(g.urls[0],/print_job_id=42/);
});
test('reloading pending work keeps its original payload without default-selection substitution',async()=>{
 const saved={payload:{mold_ids:[7],source:'batch',template_version:'mold_80x40_v1',idempotency_key:'persisted'}};let sent;
 const p=api.createSubmission({request:async(url,data)=>{sent=data;return{print_job_id:88}},navigate:()=>{},confirmReprint:()=>{throw Error('must not reconfirm')},changed:()=>{},makeKey:()=>{throw Error('must reuse')},remember:()=>{},savedAttempt:saved});
 await p.submit([],[],'mold_40x30_v1');assert.equal(sent,saved.payload);
});

test('selection page wiring displays A3 defaults and submits only checked molds',async()=>{
 const elements=new Map();const el=id=>{if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:'',disabled:false,addEventListener(type,fn){this[type]=fn}});return elements.get(id)};
 const rows=[row(209),row(211),row(256),row(213,true)],calls=[],urls=[];
 const c={URLSearchParams,Set,Map,JSON,Number,String,Array,Error,Date,Math,encodeURIComponent,console,sessionStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},crypto:{randomUUID:()=> 'id'},location:{search:'?floor_code=1F&rack_id=a&cell_id=a3',assign:url=>urls.push(url)},document:{getElementById:el,querySelectorAll:()=>[]},confirm:()=>true};
 c.window=c;c.fetch=async(url,options={})=>{calls.push({url,options});let data;
 if(url.includes('by-map-rack'))data={floor_code:'1F',rack:{rack_id:'a',mold_rack_code:'A',cells:[{id:'a3',alias:'A3'}]},items:rows,total:4,truncated:false};
 else if(url.includes('label-options'))data={items:new URL(url,'http://erp').searchParams.get('mold_ids').split(',').map(id=>rows.find(row=>row.id===Number(id))),count:4};else data={print_job_id:55};
 return{ok:true,status:200,headers:{get:()=>null},text:async()=>JSON.stringify(data),json:async()=>data}};
 vm.createContext(c);
 for(const f of ['print-recovery.js','mold-print-selection.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/assets',f),'utf8'),c);
 const html=fs.readFileSync(path.join(__dirname,'../static/mold-print-select.html'),'utf8');const script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];vm.runInContext(script,c);
 for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(el('title').textContent,'A3 · 模具标签打印');assert.match(el('preview').textContent,/3/);assert.equal(el('preview').disabled,false);
 assert.equal(calls.filter(c=>c.options.method==='POST').length,0);
 await el('preview').onclick();const posted=calls.find(c=>c.options.method==='POST');assert.deepEqual(JSON.parse(posted.options.body).mold_ids,[209,211,256]);assert.match(urls[0],/print_job_id=55/);
});

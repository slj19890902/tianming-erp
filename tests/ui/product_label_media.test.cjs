const {test}=require('node:test');
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const {parseHTML}=require('linkedom');
const media=require('../../static/ui/product-label-media.js');
const html=fs.readFileSync(require.resolve('../../static/production-packaging-label.html'),'utf8');
test('A4 physical geometry, margins, gaps and starting slot stay on paper across pages',()=>{
  const m=media.normalize({preset:'a4_4up',margin_top_mm:5,margin_bottom_mm:5,margin_left_mm:4,margin_right_mm:4,gap_x_mm:2.111,gap_y_mm:2.111,start_position:3});
  assert.equal(m.label_width_mm,99.944);assert.equal(m.label_height_mm,142.444);
  assert.ok(m.label_width_mm*2+m.gap_x_mm+8<=210);
  assert.ok(m.label_height_mm*2+m.gap_y_mm+10<=297);
  assert.equal(media.pageCount(7,m),3);
  const {document}=parseHTML(media.pages([1,2,3,4,5,6,7],m,n=>`<article>${n}</article>`));
  assert.equal(document.querySelectorAll('.label-media-sheet').length,3);
  assert.equal(document.querySelectorAll('article').length,7);
  assert.equal(document.querySelector('.label-media-sheet').children[0].className,'media-empty');
  assert.equal(document.querySelector('.label-media-sheet').children[2].textContent,'1');
  assert.match(media.css(m),/@page \{ size:210mm 297mm; margin:0/);
});
test('roll and custom sizes enforce finite physical dimensions and usable area',()=>{
  for(const [preset,w,h]of[['roll_40x30',40,30],['roll_40x80',40,80],['roll_80x40',80,40],['roll_65x45',65,45]]){
    const m=media.normalize({preset});assert.equal(m.label_width_mm,w);assert.equal(m.label_height_mm,h);assert.equal(media.pageCount(3,m),3);
  }
  for(const bad of [{preset:'unknown'},{preset:'custom',width_mm:true,height_mm:30},{preset:'custom',width_mm:Infinity,height_mm:30},{preset:'custom',width_mm:19,height_mm:30},{preset:'roll_40x30',margin_left_mm:21},{preset:'roll_40x30',start_position:2},{preset:'roll_40x30',gap_x_mm:1},{preset:'a4_4up',start_position:0}]) assert.throws(()=>media.normalize(bad));
});
const label={customer_short_name:'客户甲',product_code:'00080011641',product_name:'含<&>的产品',specification:'720×480',quantity:50,unit_label:'片',label_number:1,label_count:1};
test('adaptive fields remain in bounds, readable and escaped; drawing profile and quantity suffix are preserved',()=>{
  for(const preset of Object.keys(media.PRESETS)) {
    const m=media.normalize({preset,width_mm:70,height_mm:90});
    for(const e of media.elements(label,{},m)){
      assert.ok(e.x_mm+e.width_mm<=m.label_width_mm+.002);assert.ok(e.y_mm+e.height_mm<=m.label_height_mm+.002);
      assert.ok(e.font_size_mm>=1.8 && e.font_size_mm<=16);
    }
  }
  const rendered=media.labelHtml(label,{},media.normalize({preset:'roll_40x80'}));
  assert.ok(rendered.includes('00080011641'));assert.ok(rendered.includes('含&lt;&amp;&gt;的产品'));assert.ok(rendered.includes('50片/捆'));
  const drawing=media.labelHtml({...label,label_profile:'customer-drawing-code-quantity-v1',drawing_number:'D-007'}, {},media.normalize());
  assert.ok(drawing.includes('图号 D-007'));assert.ok(!drawing.includes('含&lt;'));
  const custom={label_layout:{layout:{elements:[{id:'quantity',visible:true,x_mm:0,y_mm:0,width_mm:40,height_mm:30,font_size_mm:4,fixed_suffix:'只供参考'}]}}};
  assert.ok(media.labelHtml(label,custom,media.normalize()).includes('50只供参考'));
});
function page(){
 const {window}=parseHTML(html);window.location={search:'?finished_stock=1&finished_lots=4:2',pathname:'/production-packaging-label.html'};window.confirm=()=>true;
 for(const select of window.document.querySelectorAll('select')) Object.defineProperty(select,'value',{configurable:true,get(){return [...this.options].find(o=>o.hasAttribute('selected'))?.value || this.options[0]?.value || '';},set(value){for(const o of this.options)if(o.value===String(value))o.setAttribute('selected','');else o.removeAttribute('selected');}});
 const calls=[];let responder=async()=>({job_id:12,job:{id:12,status:'prepared'},package:{template_version:'current_40x30_v2'}});
 const ctx={window,document:window.document,URLSearchParams,AbortController,console,requestAnimationFrame:r=>r(),fetch:async(url,opt)=>{calls.push({url,opt});return {ok:true,json:async()=>responder(url,opt)};}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../../static/ui/product-label-media.js'),'utf8'),ctx);
 const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.includes('async function preparePrintJob'));
 const exposed=`window.mediaTest={preparePrintJob,mediaInput,fillMediaInputs,changeMedia,paintMediaLabels,normalizeResponse,updateMediaLock,loadPersonalMedia,set(p){loadedPackage=plannedPackage=p;printCounts=new Map(p.plans.map(r=>[r.selection_key,r.label_count]));},reprint(jobs){loadedJobs=jobs;mediaReprintMode=true;},state(){return {mediaReprintMode,prepareAttempt};}};`;
 vm.runInNewContext(script.replace('      loadPackage();',exposed).replace('      if (mediaApi) void loadPersonalMedia();',''),ctx);
 return {window,api:window.mediaTest,calls,respond:fn=>{responder=fn;}};
}
const pkg={template_version:'current_40x30_v2',plan_fingerprint:'a'.repeat(64),lot_versions:[{lot_id:4,expected_version:2}],plans:[{selection_key:'finished-lot:4',label_count:2}],labels:[{...label,selection_key:'finished-lot:4'}],label_count:1};
test('prepared jobs bind selected media; a lost response locks edits and retries the exact request',async()=>{
 const h=page();h.api.set(pkg);h.api.fillMediaInputs({preset:'a4_4up',start_position:4});
 h.respond(async()=>{throw new TypeError('lost response');});await assert.rejects(h.api.preparePrintJob());
 const request=JSON.parse(h.calls[0].opt.body);assert.equal(request.print_media.start_position,4);assert.equal(request.print_media.preset,'a4_4up');
 assert.equal(h.window.document.querySelector('#mediaPreset').disabled,true);
 h.respond(async()=>({job_id:12,job:{id:12,status:'prepared'},package:pkg}));await h.api.preparePrintJob();
 assert.deepEqual(JSON.parse(h.calls[1].opt.body),request);
});
test('historical media reprint references the printed source and survives uncertain replay',async()=>{
 const h=page();h.api.set(pkg);h.api.reprint([{id:32,status:'printed'}]);h.api.fillMediaInputs({preset:'roll_40x80'});
 h.respond(async()=>{throw new TypeError('lost');});await assert.rejects(h.api.preparePrintJob());
 const request=JSON.parse(h.calls[0].opt.body);assert.equal(h.calls[0].url,'/api/requisition/product-label-media/reprints');assert.deepEqual(request.jobs,[{kind:'finished',id:32}]);
 h.respond(async()=>({jobs:[{id:51,status:'prepared'}],package:{...pkg,print_media:request.print_media}}));await h.api.preparePrintJob();
 assert.deepEqual(JSON.parse(h.calls[1].opt.body),request);assert.equal(h.api.state().mediaReprintMode,false);
});
test('paper preferences are scoped to the authenticated user and cannot rewrite frozen jobs',async()=>{
 const h=page(), keys=[];h.api.set(pkg);
 h.window.localStorage={getItem:key=>{keys.push(key);return JSON.stringify({last:{preset:'roll_80x40'}});}};
 h.respond(async()=>({user:{id:7}}));await h.api.loadPersonalMedia();
 assert.deepEqual(keys,['erp.productLabelMedia.v1.user.7']);assert.equal(h.api.mediaInput().preset,'roll_80x40');
 h.api.reprint([{id:8,status:'printed'}]);h.api.fillMediaInputs({preset:'roll_40x30'});await h.api.loadPersonalMedia();assert.equal(h.api.mediaInput().preset,'roll_40x30');
});
test('a delayed personal preference cannot overwrite a paper size already chosen by the operator',async()=>{
 const h=page();h.api.set(pkg);let finish;
 h.window.localStorage={getItem:()=>JSON.stringify({last:{preset:'roll_80x40'}}),setItem(){}};
 h.respond(()=>new Promise(resolve=>{finish=resolve;}));const loading=h.api.loadPersonalMedia();await new Promise(resolve=>setImmediate(resolve));
 h.api.fillMediaInputs({preset:'a4_4up'});h.api.changeMedia();
 finish({user:{id:7}});await loading;
 assert.equal(h.api.mediaInput().preset,'a4_4up');
});
test('batch response preserves nested job lifecycle when media reprints are normalized',()=>{
 const h=page();
 const normalized=h.api.normalizeResponse({package:pkg,jobs:[{kind:'finished',job_id:51,job:{id:51,status:'printed',printed_at:'2026-10-24'}}]});
 assert.equal(normalized.jobs[0].status,'printed');assert.equal(normalized.jobs[0].printed_at,'2026-10-24');
});

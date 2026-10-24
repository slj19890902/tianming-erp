const fs=require('node:fs'), assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const body=html.split('async saveStatementDispute() {')[1].split('async openStatementEdit(row) {')[0].replace(/},\s*$/,'');
const save=new AsyncFunction(body);
// Compile the shipped full template and parse inline scripts without a browser.
const vm=require('node:vm');
const vueContext=vm.createContext({console});
vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),vueContext);
const start=html.indexOf('<div id="app"');
const scriptStart=html.indexOf('<script',start);
const end=html.lastIndexOf('</div>',scriptStart);
assert.ok(start>=0 && end>start && scriptStart>end);
const errors=[];
vueContext.Vue.compile(html.slice(html.indexOf('>',start)+1,end),{decodeEntities:s=>s,onError:e=>errors.push(e.message)});
assert.deepEqual(errors,[]);
for(const match of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
  if(match[1].trim()) new vm.Script(match[1]);
}
function context(percent='13', reference='实际发票UAT-13') {
  return {statementDisputeState:{saving:false},statementDetail:{id:60,version:6,confirmation_status:'confirmed',
    items:[{source_type:'delivery',statement_item_id:741,source_customer_id:52,unit_price_snapshot:'2.9988',tax_rate_snapshot:'0',actual_received_quantity:210}],
    source_customers:[{id:52}]},
    statementDisputeForm:{reason:'实际发票确认13%税率',tax_evidence_reference:reference,remove:{},add:{},edits:{741:{unit_price:'2.9988',tax_rate_percent:percent}}},
    messages:[],showToast(message){this.messages.push(message);},closeModal(){},async loadFinance(){},errorMessage(e){return e.message;}};
}
(async()=>{
  let writes=[], confirmations=0;
  global.confirm=()=>{confirmations++;return true;};
  global.axios={post:async(url,payload)=>{writes.push({url,payload});return {data:{id:60,version:7}};}};
  const valid=context();
  assert.equal(await save.call(valid),true);
  assert.equal(confirmations,1);
  assert.deepEqual(writes[0].payload.update_lines,[{statement_item_id:741,unit_price:'2.9988',tax_rate:'0.1300'}]);
  assert.equal(writes[0].payload.tax_evidence_reference,'实际发票UAT-13');
  assert.equal(writes[0].payload.expected_version,6);
  for(const percent of ['','-1','101','13.00001','13.001','NaN','Infinity']) {
    writes=[];confirmations=0;
    const state=context(percent);
    assert.equal(await save.call(state),false,`must reject ${percent}`);
    assert.equal(writes.length,0);assert.equal(confirmations,0);
    assert.equal(state.statementDisputeForm.edits[741].tax_rate_percent,percent);
  }
  writes=[];confirmations=0;
  const noEvidence=context('13','   ');
  assert.equal(await save.call(noEvidence),false);
  assert.equal(writes.length,0);assert.equal(confirmations,0);
  assert.match(noEvidence.messages[0],/税率更正须填写实际票据或确认依据/);
  const cancelled=context();global.confirm=()=>false;
  assert.equal(await save.call(cancelled),false);assert.equal(writes.length,0);
  // Failure must preserve the exact rate, source reference and original bill version.
  global.confirm=()=>true;global.axios={post:async()=>{throw Error('版本冲突');}};
  const failed=context();
  assert.equal(await save.call(failed),false);
  assert.equal(failed.statementDetail.version,6);
  assert.equal(failed.statementDisputeForm.edits[741].unit_price,'2.9988');
  assert.equal(failed.statementDisputeForm.tax_evidence_reference,'实际发票UAT-13');
  console.log('PASS actual tax correction UI: 13% payload, exact prices, evidence, bounds, cancel and failed-save state');
})().catch(error=>{console.error(error);process.exitCode=1;});

import json
from pathlib import Path
from test_p1_09c_96_quotation_action_guard import _method_body, _run_node


def test_preview_history_and_retry_bind_to_current_input(tmp_path: Path):
    definitions = {
        'refreshQuotationLine': ('line', True, 'async refreshQuotationLine(line) {', 'quotationPayload() {'),
        'quotationPreviewInput': ('line', False, 'quotationPreviewInput(line) {', 'async refreshQuotationLine(line) {'),
        'loadQuotationHistory': ('', True, 'async loadQuotationHistory() {', 'onQuotationMaterialSelected(line) {'),
        'quotationCommand': ('owner,action,body', False, 'quotationCommand(owner, action, body) {', 'quotationCommandFailed(owner, action, pending, error) {'),
    }
    sources = {key: ('async ' if async_ else '') + 'function(' + args + '){' + _method_body(start, end) + '}'
               for key,(args,async_,start,end) in definitions.items()}
    _run_node('''
const assert=require('node:assert/strict');
const requests=[],histories=[];
const axios={post:(_,body)=>new Promise((resolve,reject)=>requests.push({body,resolve,reject})),
  get:()=>new Promise((resolve,reject)=>histories.push({resolve,reject}))};
const sources=SOURCE;
const vm={quotationCustomer:{id:1},authGeneration:1,activePage:'quotations',errorMessage:e=>e.message};
for(const [key,body] of Object.entries(sources))vm[key]=new Function('axios','return '+body)(axios).bind(vm);
const line={box_type:'A1',length_mm:100,width_mm:100,height_mm:100,material_id:1,_final_manual:false};
vm.quotationDraft={items:[line]};
(async()=>{
  const old=vm.refreshQuotationLine(line); line.length_mm=200; const latest=vm.refreshQuotationLine(line);
  requests[1].resolve({data:{suggested_unit_price:20}});await latest;
  requests[0].resolve({data:{suggested_unit_price:10}});await old;assert.equal(line.final_unit_price,20);
  const manual=vm.refreshQuotationLine(line);line._final_manual=true;line.final_unit_price=37;
  requests[2].resolve({data:{suggested_unit_price:30}});await manual;assert.equal(line.final_unit_price,37);
  const deleted=vm.refreshQuotationLine(line);vm.quotationDraft.items=[];
  requests[3].resolve({data:{suggested_unit_price:40,estimated_unit_cost:40}});await deleted;assert.notEqual(line.estimated_unit_cost,40);
  vm.quotationDraft={items:[line]};const closed=vm.refreshQuotationLine(line);vm.quotationDraft={items:[]};
  requests[4].reject(new Error('stale failure'));await closed;assert.notEqual(line._message,'stale failure');
  const one=vm.loadQuotationHistory();vm.quotationCustomer={id:2};const two=vm.loadQuotationHistory();
  histories[1].resolve({data:{items:[{id:2}]}});await two;histories[0].resolve({data:{items:[{id:1}]}});await one;
  assert.equal(vm.quotationHistory[0].id,2);
  const signedOut=vm.loadQuotationHistory();vm.authGeneration++;
  histories[2].resolve({data:{items:[{id:99}]}});await signedOut;assert.equal(vm.quotationHistory[0].id,2);
  const draft={},body={items:[{price:2}]};const first=vm.quotationCommand(draft,'save',body);
  assert.strictEqual(vm.quotationCommand(draft,'save',body),first);body.items[0].price=5;
  assert.equal(first.payload.items[0].price,2);first.uncertain=true;
  assert.throws(()=>vm.quotationCommand(draft,'save',body),/核实/);
  body.items[0].price=2;assert.strictEqual(vm.quotationCommand(draft,'save',body),first);
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('SOURCE', json.dumps(sources, ensure_ascii=False)), tmp_path, 'quotation-reliable.js')

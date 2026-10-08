'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const test=require('node:test'),assert=require('node:assert/strict');
const ts=require(process.env.ERP_FRONTEND_DEPS+'/typescript');
const source=fs.readFileSync(path.join(__dirname,'../src/utils/formalOrderEntry.ts'),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const exportsBox={}; vm.runInNewContext(js,{exports:exportsBox,crypto:require('node:crypto').webcrypto});
const {parseFormalOrderEntry,formalOrderEntryQuery,currentFormalFrame}=exportsBox;
test('only exact entry names and scalar UUID request identities are accepted',()=>{
  for(const action of ['new','import','email']) {
    const query=formalOrderEntryQuery(action);
    assert.equal(parseFormalOrderEntry(query.order_entry,query.order_request).action,action);
  }
  for(const action of ['save','sync','__proto__',undefined,['new']]) assert.equal(parseFormalOrderEntry(action,require('node:crypto').randomUUID()),null);
  for(const id of ['',null,[],{},'not-a-request','abc%20def']) assert.equal(parseFormalOrderEntry('new',id),null);
});
test('active route selects its own frame regardless of cached-frame order',()=>{
  const first={dataset:{formalRoute:'/warehouse'},contentWindow:{}},second={dataset:{formalRoute:'/orders'},contentWindow:{}};
  const doc={querySelectorAll:()=>[first,second]};
  assert.equal(currentFormalFrame(doc,'/orders'),second.contentWindow);
  assert.equal(currentFormalFrame(doc,'/warehouse'),first.contentWindow);
  assert.equal(currentFormalFrame(doc,'/review/orders'),null);
});

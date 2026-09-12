const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('static/index.html','utf8');
const match=/^([\t ]*)async handleEmailPdf\(/m.exec(source);
const end=source.indexOf('\n'+match[1]+'},',match.index)+('\n'+match[1]+'},').length;
let calls=0,confirm=true,fail=false;
const context={window:{confirm:()=>confirm},axios:{post:async()=>{calls++;if(fail)throw Error('failed')}}};vm.createContext(context);
const method=vm.runInContext('({'+source.slice(match.index,end)+'})',context).handleEmailPdf;
function state(){let draft={file_hash:'a',source_name:'fixture.pdf',email_attachment_id:1};return {draft,orderImportDrafts:[draft],emailQueueExcluded:[],refreshEmailQueueCount:async()=>{},showToast:()=>{},errorMessage:e=>e.message};}
(async()=>{let x=state();confirm=false;await method.call(x,x.draft,'deleted');assert.equal(calls,0);assert.equal(x.orderImportDrafts.length,1);confirm=true;await method.call(x,x.draft,'deleted');assert.equal(calls,1);assert.equal(x.orderImportDrafts.length,0);assert.equal(x.emailQueueExcluded[0].reason,'deleted');x=state();fail=true;await method.call(x,x.draft,'processed');assert.equal(x.orderImportDrafts.length,1);assert.equal(x.draft._email_handling,false);x.loading=true;await method.call(x,x.draft,'processed');assert.equal(calls,2);console.log('email disposition: cancel, delete, failure preservation, busy guard passed');})();

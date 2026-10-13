const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/ui/entry-product-editor.js','utf8');
const body=source.slice(source.indexOf('  async function api('),source.indexOf('  const costPath='));
for(const status of [500,502,200])test(`non-JSON response preserves HTTP ${status}`,async()=>{
 const fetch=async()=>({ok:status===200,status,json:async()=>{throw new SyntaxError('Unexpected token I')}});
 const api=vm.runInNewContext(`(${body.trim()})`,{fetch,Error,JSON});
 await assert.rejects(api('/api/warehouse/example'),e=>e.status===status&&e.message.includes('已保留')&&!e.message.includes('Unexpected token'));
});
test('JSON refusal remains a refusal with its business detail',async()=>{
 const api=vm.runInNewContext(`(${body.trim()})`,{fetch:async()=>({ok:false,status:409,json:async()=>({detail:'版本已变化'})}),Error,JSON});
 await assert.rejects(api('/api/master/products/1'),e=>e.status===409&&e.message==='版本已变化');
});

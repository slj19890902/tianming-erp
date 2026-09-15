const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('static/index.html','utf8');
function method(name,args){const m=html.match(new RegExp('          '+name+'\\('+args+'\\) \\{([\\s\\S]*?)\\n          \\},'));assert(m,name);return new Function(...args.split(',').map(x=>x.trim()),m[1]);}
const reason=method('importItemMasterBlockReason','item, index');
const missing={matched_product_id:1,product_code:'TEST',readiness:{order_save_missing_labels:['主料报料长未填写','材质供应商未填写']}};
assert(reason(missing,1).includes('第2行【TEST】'));
assert(reason(missing,1).includes('材质供应商'));
assert.equal(reason({...missing,readiness:{order_save_missing_labels:[]}},0),'');
assert(reason({matched_product_id:1},0).includes('最新报料资料'));
assert(reason({is_new_product:true},0).includes('登记'));
const normalize=method('commonBoxReadiness','source');
assert.deepEqual(normalize(missing).order_save_missing_labels,missing.readiness.order_save_missing_labels);
assert(reason({...missing,_common_box_readiness:normalize(missing)},1).includes('第2行'));
for(const s of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi))if(s[1].trim()&&!s[0].includes('application/json'))new vm.Script(s[1]);
const sandbox={console};vm.createContext(sandbox);vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),sandbox);
const errors=[];sandbox.Vue.compile(html.match(/<body[^>]*>([\s\S]*?)<script/)[1],{decodeEntities:s=>s.replace(/&gt;/g,'>').replace(/&lt;/g,'<').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&'),onError:e=>errors.push(e.message)});assert.deepEqual(errors,[]);
console.log('PDF master gates, line/code reasons, refresh transport, JS and Vue passed');

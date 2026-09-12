import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
const html=fs.readFileSync(new URL("../../../static/index.html",import.meta.url),"utf8");
test("ERP inline scripts and receipt preference template compile",()=>{
  for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))if(match[1].trim())new vm.Script(match[1]);
  const context={console};vm.createContext(context);
  vm.runInContext(fs.readFileSync(new URL("../../../static/vendor/vue-3.5.40.global.prod.js",import.meta.url),"utf8"),context);
  const start=html.indexOf('<details v-if="productForm.id" :key="`storage-');
  assert.ok(start>0);
  const end=html.indexOf('</details>',start)+10;
  const errors=[];
  context.Vue.compile(html.slice(start,end),{decodeEntities:raw=>raw,onError:e=>errors.push(e.message)});
  assert.deepEqual(errors,[]);
  assert.ok(html.includes('idempotency_key:`receipt-storage:${id}:${createIdempotencyKey()}`'));
});

'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const test=require('node:test'),assert=require('node:assert/strict'),ts=require(process.env.ERP_UI_TYPESCRIPT_LIBRARY);
const source=fs.readFileSync(path.join(__dirname,'../src/App.vue'),'utf8');
const body=source.match(/function activateTabPath\(path: string\) \{([\s\S]*?)\n\}/)[0];
function setup(){const calls=[],tabsStore={tabs:[{path:'/'},{path:'/review/orders/native-new'},{path:'/review/orders'}]},route={path:'/review/orders'};const context={tabsStore,route,router:{push:p=>calls.push(p)}};vm.runInNewContext(ts.transpileModule(body,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText+';this.activateTab=activateTabPath',context);return {calls,context}}
test('explicit tab label only navigates to its registered workspace',()=>{const h=setup();h.context.activateTab('/review/orders/native-new');assert.deepEqual(h.calls,['/review/orders/native-new']);assert.match(source,/@click.stop="activateTabPath\(tab.path\)"/)});
test('unregistered and current pane do not cause navigation or draft reset',()=>{const h=setup();h.context.activateTab('/unknown');h.context.activateTab('/review/orders');assert.equal(h.calls.length,0)});

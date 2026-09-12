import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
const html=fs.readFileSync(new URL('../../../static/index.html',import.meta.url),'utf8');
function method(name){
 const rx=new RegExp(`(?:async\\s+)?${name}\\([^)]*\\)\\s*\\{`);const m=rx.exec(html);assert.ok(m,name);
 const tail=html.slice(m.index);const next=/\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{/.exec(tail.slice(m[0].length));
 return tail.slice(0,m[0].length+next.index).trim().replace(/,$/,'');
}
const names=['deliveryUnifiedKey','deliveryUnifiedInForm','deliveryUnifiedSelected','deliveryUnifiedSelectedCount','toggleDeliveryUnifiedRow','deliveryUnifiedMax','deliveryUnifiedQuantity','selectDeliveryUnifiedQuantity','setDeliveryUnifiedQuantity','deliveryUnifiedLines','setDeliveryBatchSelection','unorderedFinishedLotId','unorderedFinishedAvailableQuantity','unorderedFinishedSelection','setUnorderedFinishedSelection','isDeliveryItemAlreadyInForm','isUnorderedFinishedLotInForm','deliveryDefaultQuantity','toggleOrderCommonBoxSelection','isOrderCommonBoxSelected','setOrderCommonBoxQuantity'];
function context(){return Object.assign({deliveryForm:{lines:[]},deliveryBatchPicker:{loading:false,selected:{}},unorderedFinishedPicker:{selected:{}},orderCommonBoxPicker:{selected:{},selection_sequence:0}},vm.runInNewContext(`({${names.map(method).join(',')}})`));}
test('row toggle and repeated quantity focus preserve selection and value for both sources',()=>{
 const ctx=context();
 for(const item of [{order_item_id:4,source_type:'order',remaining_quantity:12},{inventory_lot_id:4,source_type:'unordered_finished',available_quantity:20}]){
 ctx.toggleDeliveryUnifiedRow(item);assert.equal(ctx.deliveryUnifiedSelected(item),true);
 ctx.toggleDeliveryUnifiedRow(item);assert.equal(ctx.deliveryUnifiedSelected(item),false);
 ctx.selectDeliveryUnifiedQuantity(item);ctx.setDeliveryUnifiedQuantity(item,'5');ctx.selectDeliveryUnifiedQuantity(item);
 assert.equal(ctx.deliveryUnifiedQuantity(item),'5');assert.equal(ctx.deliveryUnifiedSelected(item),true);
 }
 assert.equal(ctx.deliveryUnifiedSelectedCount(),2);
});
test('common-box click/focus selects once and does not overwrite quantity',()=>{
 const ctx=context(), row={id:7};ctx.toggleOrderCommonBoxSelection(row,true);ctx.setOrderCommonBoxQuantity(row,'8');ctx.toggleOrderCommonBoxSelection(row,true);
 assert.equal(ctx.orderCommonBoxPicker.selected['7'].quantity,'8');ctx.toggleOrderCommonBoxSelection(row,false);assert.equal(ctx.isOrderCommonBoxSelected(7),false);
});
test('inline JS parses and Vue template compiles with input events isolated from row clicks',()=>{
 const script=html.slice(html.indexOf('<script>')+8,html.lastIndexOf('</script>'));new vm.Script(script);
 const sandbox={console};vm.createContext(sandbox);vm.runInContext(fs.readFileSync(new URL('../../../static/vendor/vue-3.5.40.global.prod.js',import.meta.url),'utf8'),sandbox);
 const template=html.slice(html.indexOf('<div id="app"'),html.indexOf('<script>',html.indexOf('<div id="app"')));
 sandbox.Vue.compile(template,{decodeEntities:s=>s,onError:e=>{throw e;}});
 assert.ok(html.includes('@click.stop="selectDeliveryUnifiedQuantity(item)"'));
 assert.ok(html.includes('@click.stop="toggleOrderCommonBoxSelection(row,true)"'));
});

const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('assert/strict');
const root=process.argv[2],html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const scripts=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
 .map(x=>x[1]).filter(x=>x.trim());
const box={axios:{defaults:{},interceptors:{request:{use(){}},response:{use(){}}}},
 Vue:{createApp(d){box.d=d;return {component(){return this},mount(){return this}}}},
 window:{},document:{},localStorage:{getItem(){return ''},setItem(){},removeItem(){}},
 TMOrderReference:{component:{}},URLSearchParams,setTimeout,clearTimeout,console};
vm.createContext(box);vm.runInContext(fs.readFileSync(path.join(root,'static/ui/incoming-actual.js'),'utf8'),box);vm.runInContext(scripts[0].slice(0,scripts[0].indexOf('window.TmDeliveryEditorLayout.install(app);')),box);
vm.runInContext(fs.readFileSync(path.join(root,'static/ui/order-context.js'),'utf8'),box);
let mixin;box.window.ERPOrderContext.install({component(){},mixin(value){mixin=value}});
const ctx={...box.d.methods,...mixin.methods,
 commonBoxReadiness:()=>({ready:true}),materialName:()=>'',invalidateLineInventory(){throw Error('changed stock basis')}};
const p={id:328,product_code:'21302055',product_name:'满衬板',length_mm:890,width_mm:440,
 customer_specification:'89.5*44cm G9G/B R'};
assert.equal(ctx.customerProductSpecification(p),'89.5*44cm G9G/B R');
assert.equal(ctx.spec(p),'890×440mm');
assert.equal(ctx.customerProductSpecification({...p,customer_specification:''}),'');
assert.equal(ctx.customerProductSpecification({...p,customer_specification:null}),'890×440mm');
const line={quantity:270,unit_price:'1.670',_inventory_product:{...p,customer_specification:'old'}};
assert.equal(ctx.applyOrderContextProduct(line,p,false),false);
assert.equal(line.specification,'89.5*44cm G9G/B R');
assert.equal(line.quantity,270);assert.equal(line.unit_price,'1.670');
console.log('customer text preserved without changing quantity, price, or stock basis');

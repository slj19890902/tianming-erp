const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const from=html.indexOf('          applyPartnerProductDefaults() {'),to=html.indexOf('          onProductProcessesChanged(',from);
const methods=vm.runInNewContext('({'+html.slice(from,to)+'})');
const state={...methods,activeCustomerOptions:[{id:137,name:'研光'}],productForm:{customer_id:137,box_style:'A1/0201',layer_count:5,flute_type:null,_production_processes:['无需结合']},productBoxTypeRule:()=>({code:'a1_0201'}),loadProductBoxTypeRules:async()=>{}};
state.applyPartnerProductDefaults();assert.equal(state.productForm.production_label_enabled,true);assert.equal(state.productForm.production_label_units_per_label,5);assert.equal(state.productForm.flute_type,'AB');assert.ok(state.productForm._production_processes.includes('打钉'));
state.productForm.production_label_enabled=false;state.onProductProductionLabelToggle();state.applyPartnerProductDefaults();assert.equal(state.productForm.production_label_enabled,false);
state.productForm={id:1,customer_id:137,production_label_enabled:false};state.applyPartnerProductDefaults();assert.equal(state.productForm.production_label_enabled,false);
console.log('default checkbox and explicit cancellation passed');

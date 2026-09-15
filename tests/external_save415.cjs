const fs=require('fs'),path=require('path'),assert=require('assert'),vm=require('vm');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
function getter(name){const m=html.match(new RegExp('          '+name+'\\(\\) \\{([\\s\\S]*?)\\n          \\},'));assert(m,name);return new Function(m[1]);}
const names=['productCreaseMismatch','productMoldError','productPrintingPlateError','productPrintingColorError'];
function context(mode){return {modal:{type:'product'},productForm:{id:3719,supply_mode:mode,mold_tool_id:null,printing_plate_mode:'plate',_printing_colors_touched:true,production_label_enabled:true,production_label_units_per_label:null},productReportCreaseTouched:()=>true,validateProductCreaseAndReport:()=> '压线不一致',productUsesMold:()=>true,productHasPrinting:()=>true,productPrintingPlateCount:()=>1,productUsesDirectPrinting:()=>true,productDirectPrintingColorCount:()=>1,productPrintingColorsForWrite:()=>[]};}
for(const name of names){assert.equal(getter(name).call(context('external_purchase')),'',name+' must ignore hidden production fields');for(const mode of ['corrugated_production','mixed_bom']){const c=context(mode);c.productPrintingPlateContent=()=> '挂板';assert(getter(name).call(c),name+' must still protect '+mode);}}
const ctx=context('corrugated_production');ctx.productForm.mold_tool_id=42;assert.equal(getter('productMoldError').call(ctx),'');
const button=html.match(/<button[^>]*class="btn primary product-inline-save"[^>]*>/)[0];
const condition=button.match(/:disabled="([^"]+)"/)[1];
const external=context('external_purchase');Object.assign(external,{canEditProducts:true,loading:false,masterSavePending:false});
for(const name of names) external[name]=getter(name).call(external);
external.productProductionLabelError=getter('productProductionLabelError').call(external);
assert(external.productProductionLabelError,'External labels still require a quantity');
assert(vm.runInNewContext(condition,external));
external.productForm.production_label_units_per_label=50;
external.productProductionLabelError=getter('productProductionLabelError').call(external);
assert.equal(vm.runInNewContext(condition,external),false);
for(const name of ['loading','masterSavePending']){external[name]=true;assert(vm.runInNewContext(condition,external));external[name]=false;}
external.canEditProducts=false;assert(vm.runInNewContext(condition,external));
assert.equal(getter('productSaveBlockReason').call(external),'当前账号没有编辑权限');
external.canEditProducts=true;external.productMoldError='需要模具';
assert.equal(getter('productSaveBlockReason').call(external),'需要模具');
external.productMoldError='';assert.equal(getter('productSaveBlockReason').call(external),'');
for(const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi))if(script[1].trim()&&!script[0].includes('application/json'))new vm.Script(script[1]);
const sandbox={console};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/vendor/vue-3.5.40.global.prod.js'),'utf8'),sandbox);
const errors=[];
sandbox.Vue.compile(html.match(/<body[^>]*>([\s\S]*?)<script/)[1],{decodeEntities:s=>s.replace(/&gt;/g,'>').replace(/&lt;/g,'<').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&'),onError:e=>errors.push(e.message)});
assert.deepEqual(errors,[]);
console.log('External hidden-field, production/mixed requirements, actual save button, permission/busy and JS syntax checks passed');


const fs = require("fs");
const vm = require("vm");

const indexPath = process.argv[2];
const html = fs.readFileSync(indexPath, "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1])
  .filter(source => source.trim());
if (scripts.length !== 1) {
  throw new Error(`Expected one inline application script, found ${scripts.length}`);
}

const sandbox = {
  axios: {
    defaults: {},
    interceptors: { response: { use() {} } },
  },
  Vue: {
    createApp(definition) {
      sandbox.definition = definition;
      return {
        component() { return this; },
        mount() { return this; },
      };
    },
  },
  localStorage: {
    getItem() { return ""; },
    setItem() {},
    removeItem() {},
  },
  window: {},
  console,
  URLSearchParams,
  setTimeout,
  clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(scripts[0], sandbox);

const methods = sandbox.definition.methods;
const computed = sandbox.definition.computed;
const context = { ...methods };

Object.defineProperty(context,'pdfImportSelectionBusy',{get(){return computed.pdfImportSelectionBusy.call(context)}});
context.loading=true;
context.orderImportBatch={status:'recognized'};
context.orderImportDrafts=[{preview_safety_token:'fixture',matched_customer_id:5,_reminder_loaded:true,integrity_check:{integrity_status:'passed'},_save_status:'idle',items:[{matched_product_id:1,quantity:22,unit_price:'1'}]}];
context.orderImportReminderSignature=()=>'';
context.inventoryDecisionRequired=()=>'';
context.showToast=()=>{};
if(computed.confirmableImportDraftCount.call(context)!==1)throw Error('Valid draft must be selectable');
if(context.pdfImportSelectionBusy)throw Error('Background loading must not block selection');
methods.toggleConfirmableImportDrafts.call(context);
if(!context.orderImportDrafts[0].confirmed)throw Error('Selection did not work');
context.orderImportBatch.status='recognizing';
methods.toggleConfirmableImportDrafts.call(context);
if(!context.orderImportDrafts[0].confirmed)throw Error('Recognizing must block selection');
context.orderImportBatch.status='recognized';context.orderImportDrafts[0]._save_status='saving';
methods.toggleConfirmableImportDrafts.call(context);
if(!context.orderImportDrafts[0].confirmed)throw Error('Saving must block selection');
context.orderImportDrafts[0]._save_status='idle';context.orderImportDrafts[0].preview_safety_token=null;
if(computed.confirmableImportDraftCount.call(context)!==0)throw Error('Missing safety token must remain blocked');
console.log('selection state checks passed');

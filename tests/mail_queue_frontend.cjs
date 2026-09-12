
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


const assert=require('assert');
(async()=>{
  const calls=[];
  sandbox.axios.put=async(path,payload)=>{calls.push({path,payload});return {data:{saved:true}}};
  const settings={version:4,configured:true,sender_addresses:['one@example.com'],sync_interval_minutes:5,automatic_enabled:true};
  sandbox.axios.get=async(path)=>({data:path.endsWith('/settings')?settings:{pending:3,ready:2,improve:1,recognizing:0}});
  Object.assign(context,{user:{role:'admin'},canCreateOrders:true,emailSettings:{...settings},emailSenderText:'Two@Example.com; three@example.com',emailSettingsSaving:false,systemSectionLoading:{},systemSectionLoaded:{},systemSectionErrors:{},showToast(){},errorMessage:e=>e.message});
  await methods.saveEmailSettingsPart.call(context,'senders');
  assert.equal(calls[0].path,'/api/email-intake/senders');assert.equal(calls[0].payload.expected_version,4);
  assert.equal(calls[0].payload.addresses.join(','),'Two@Example.com,three@example.com');
  assert.equal(context.emailSettingsSaving,false);assert.equal(context.systemSectionLoaded.email,true);
  await methods.refreshEmailQueueCount.call(context);assert.equal(context.emailQueueCount,3);
  sandbox.axios.post=async()=>({data:{drafts:[{source_name:'one.pdf',email_queue_status:'ready',email_attachment_id:1,matched_customer_id:5,items:[{matched_product_id:1}]}]}});
  context.openOrderPdfImport=()=>{context.modal={type:'orderPdfImport'};context.orderImportBatch={status:'idle'}};
  context.prepareImportDraftReminderState=d=>d;context.preparePdfImportItem=i=>i;
  context.loadOrderImportReminders=async()=>{};context.loadOrderLineInventory=async()=>{};
  await methods.openEmailQueue.call(context);
  assert.equal(context.emailQueueMode,true);assert.equal(context.orderImportBatch.status,'recognized');assert.equal(context.orderImportDrafts[0].confirmed,false);
  assert.equal(context.emailQueueCount,3,'Opening cannot clear pending badge');
  context.user={role:'sales'};await methods.refreshEmailQueueCount.call(context);assert.equal(context.emailQueueCount,0);
  console.log('email settings and queue frontend checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});

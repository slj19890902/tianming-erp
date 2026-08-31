from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"


def _run_node(tmp_path: Path, name: str, source: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-65A frontend behavior tests"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target), str(INDEX)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _vue_harness(body: str) -> str:
    return f"""
const fs = require("fs");
const vmModule = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...html.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/g)].map(match => match[1]).filter(Boolean);
if (scripts.length !== 1) throw new Error(`expected one inline script, got ${{scripts.length}}`);
const sandbox = {{
  axios: {{defaults:{{}},interceptors:{{response:{{use(){{}}}}}}}},
  Vue: {{createApp(definition) {{ sandbox.definition=definition; return {{component(){{return this;}},mount(){{return this;}}}}; }}}},
  localStorage: {{getItem(){{return "";}},setItem(){{}},removeItem(){{}}}},
  window: {{location:{{search:"",pathname:"/"}},history:{{replaceState(){{}}}},addEventListener(){{}},removeEventListener(){{}}}},
  document: {{getElementById(){{return null;}},querySelector(){{return null;}},addEventListener(){{}},removeEventListener(){{}},body:{{classList:{{add(){{}},remove(){{}}}}}}}},
  navigator: {{}}, console, URLSearchParams, AbortController, setTimeout, clearTimeout, confirm:()=>true,
}};
vmModule.createContext(sandbox);
vmModule.runInContext(scripts[0],sandbox);
const methods=sandbox.definition.methods;
const assert=(condition,message)=>{{if(!condition)throw new Error(message);}};
{body}
"""


def test_receipt_memo_is_internal_optional_and_absent_from_print_pages() -> None:
    source = INDEX.read_text(encoding="utf-8")
    receipt_panel = source[
        source.index('aria-label="内部履约备忘"') :
        source.index('<div v-else-if="modal.type === \'delivery\'">')
    ]
    assert "不会出现在客户打印、对账或开票资料中" in receipt_panel
    assert "本次没有备忘时可直接保存回单，不增加额外步骤" in receipt_panel
    assert 'receiptForm.reminder_drafts || []' in receipt_panel
    assert "仅本次回单" in receipt_panel
    assert "该客户的指定常用箱" in receipt_panel
    assert "一次性（需人工处理）" in receipt_panel
    assert "持续提醒（需人工结束）" in receipt_panel
    assert "本次已处理" in receipt_panel
    assert "结束持续提醒" in receipt_panel

    for path in (ROOT / "static").glob("*.html"):
        if path.name == "index.html":
            continue
        public_source = path.read_text(encoding="utf-8")
        assert "内部履约备忘" not in public_source, path.name
        assert "fulfillment-reminder" not in public_source, path.name


def test_new_receipt_drafts_are_atomic_optional_and_reuse_uncertain_key(tmp_path: Path) -> None:
    body = r"""
let posts=[];
let failNext=false;
sandbox.axios.post=async(url,payload)=>{posts.push({url,payload});if(failNext){failNext=false;throw new Error("network uncertain");}return {data:{id:301,status:"confirmed"}};};
sandbox.axios.put=async()=>({data:{}});
const messages=[];
const baseForm=()=>({id:null,delivery_id:21,actual_received_date:"2026-08-16",reconciliation_month:"2026-08",signed_by:"客户签收",reminder_drafts:[],items:[{delivery_item_id:101,product_code:"P1",product_name:"外箱",delivered_quantity:10,actual_received_quantity:10,resolution_action:"",difference_reason:"",return_location_id:null,expected_return_layout_version:null}]});
const state=()=>({loading:false,error:"",items:[],total:0,page:1,historyMode:false,editorVisible:true,saving:false,actionId:null,productsLoading:false,productsError:"",products:[{id:7,product_code:"P-007",product_name:"三层外箱",can_select:true}],productQuery:"",bundleAttempt:null,mutationAttempt:null});
const app={...methods,user:{id:1},authGeneration:0,modal:{type:"receipt"},deliveries:[{id:21,delivery_number:"TH-21"}],deliveryOperationState:{action:""},receiptOperationState:{action:""},receiptForm:baseForm(),receiptReminderState:state(),receiptReminderEditor:{id:null,version:null,local_key:"",scope_type:"product",reminder_type:"replenishment",content:"下次送货补 2 只",suggested_quantity:2,cadence:"one_time",remind_on:"2026-08-20",product_id:7},validateReceiptForm(){return "";},showToast(message,danger=false){messages.push({message,danger});},invalidateDeliveryListDetail(){},closeModal(){this.modal=null;},async loadDeliveries(){return true;},async loadOrders(){return true;},async loadKpi(){return true;},async loadFinance(){return true;}};
(async()=>{
  const drafted=await app.saveReceiptReminder();
  assert(drafted===true,"local reminder draft failed");
  assert(posts.length===0,"drafting a new receipt reminder wrote before receipt save");
  assert(app.receiptForm.reminder_drafts.length===1,"draft not retained in receipt form");

  failNext=true;
  const uncertain=await app.saveReceipt();
  assert(uncertain===false,"uncertain receipt mutation was reported as success");
  const firstKey=posts[0].payload.reminder_bundle_idempotency_key;
  assert(posts[0].payload.reminders.length===1 && firstKey,"receipt and reminders were not sent atomically");
  const retried=await app.saveReceipt();
  assert(retried===true,"same receipt retry did not succeed");
  assert(posts[1].payload.reminder_bundle_idempotency_key===firstKey,"same uncertain payload did not reuse its key");

  app.modal={type:"receipt"};app.receiptForm=baseForm();app.receiptReminderState=state();
  const noMemo=await app.saveReceipt();
  assert(noMemo===true,"receipt without memo should still save normally");
  const plain=posts[2].payload;
  assert(!Object.prototype.hasOwnProperty.call(plain,"reminders"),"empty receipt invented a reminders field");
  assert(!Object.prototype.hasOwnProperty.call(plain,"reminder_bundle_idempotency_key"),"empty receipt invented an extra idempotency step");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(tmp_path, "p1-65a-atomic-draft.js", _vue_harness(body))


def test_existing_reminder_mutations_reuse_keys_and_keep_cas(tmp_path: Path) -> None:
    body = r"""
let requests=[];
let rejectFirst=true;
sandbox.axios.put=async(url,payload)=>{requests.push({method:"put",url,payload});if(rejectFirst){rejectFirst=false;throw new Error("uncertain");}return {data:{id:9,version:4}};};
sandbox.axios.post=async(url,payload)=>{requests.push({method:"post",url,payload});return {data:{id:9,version:4}};};
const app={...methods,user:{id:1},authGeneration:0,modal:{type:"receipt"},receiptForm:{id:5,delivery_id:21,reminder_drafts:[]},receiptReminderState:{loading:false,error:"",items:[],total:0,page:1,historyMode:false,editorVisible:true,saving:false,actionId:null,productsLoading:false,productsError:"",products:[],productQuery:"",bundleAttempt:null,mutationAttempt:null},receiptReminderEditor:{id:9,version:3,local_key:"",scope_type:"customer",reminder_type:"delivery_attention",content:"卸货前联系仓库",suggested_quantity:null,cadence:"continuous",remind_on:"",product_id:null},showToast(){},async loadReceiptReminders(){return true;},cancelReceiptReminderEditor(){this.receiptReminderState.editorVisible=false;}};
(async()=>{
  const failed=await app.saveReceiptReminder();
  assert(failed===false,"failed update was reported as success");
  const key=requests[0].payload.idempotency_key;
  assert(requests[0].payload.expected_version===3,"edit omitted expected version");
  const retried=await app.saveReceiptReminder();
  assert(retried===true && requests[1].payload.idempotency_key===key,"same update retry did not reuse key");
  const row={id:9,version:4,cadence:"continuous"};
  await app.transitionReceiptReminder(row,"resolve");
  assert(requests[2].url.endsWith("/fulfillment-reminders/9/resolve"),"continuous reminder did not use explicit resolve endpoint");
  assert(requests[2].payload.expected_version===4 && requests[2].payload.idempotency_key,"resolve omitted CAS or idempotency");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(tmp_path, "p1-65a-mutations.js", _vue_harness(body))


def test_reminder_reads_are_latest_wins_and_preserve_last_good(tmp_path: Path) -> None:
    body = r"""
const pending=[];
sandbox.axios.get=(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));
const app={...methods,user:{id:1},authGeneration:0,modal:{type:"receipt"},receiptForm:{id:5,delivery_id:21},receiptReminderState:{loading:false,error:"",items:[{id:100,content:"last-good"}],total:1,page:1,historyMode:false,editorVisible:false,saving:false,actionId:null,productsLoading:false,productsError:"",products:[],productQuery:"",bundleAttempt:null,mutationAttempt:null},showToast(){},resetPagePerformanceState(){},resetModalA11ySession(){},errorMessage(error){return error.message;}};
(async()=>{
  const oldProducts=app.loadReceiptReminderProducts("old");
  app.receiptForm={id:6,delivery_id:22};
  const newProducts=app.loadReceiptReminderProducts("new");
  pending[0].resolve({data:{items:[{id:1,product_code:"OLD"}]}});
  pending[1].resolve({data:{items:[{id:2,product_code:"NEW"}]}});
  await Promise.all([oldProducts,newProducts]);
  assert(app.receiptReminderState.products.length===1 && app.receiptReminderState.products[0].product_code==="NEW","old receipt products overwrote the current modal");

  app.receiptReminderState.items=[{id:100,content:"last-good"}];
  const failed=app.loadReceiptReminders({history:false,page:1});
  pending[2].reject(new Error("temporary outage"));
  assert(await failed===false,"failed list read was reported as success");
  assert(app.receiptReminderState.items[0].content==="last-good","failed refresh cleared last-good reminders");
  assert(app.receiptReminderState.error.includes("temporary outage"),"failed refresh did not expose retryable error");
})().catch(error=>{console.error(error);process.exit(1);});
"""
    _run_node(tmp_path, "p1-65a-latest-wins.js", _vue_harness(body))


def test_logout_reset_clears_all_receipt_reminder_text_and_attempts() -> None:
    source = INDEX.read_text(encoding="utf-8")
    reset = source[
        source.index("resetPagePerformanceState() {") :
        source.index("pageCacheFresh(page) {", source.index("resetPagePerformanceState() {"))
    ]
    assert 'this.receiptForm = {items:[],reminder_drafts:[]}' in reset
    assert "receiptReminderState = {loading:false,error:\"\",items:[]" in reset
    assert "bundleAttempt:null" in reset
    assert "mutationAttempt:null" in reset
    assert "this.receiptReminderEditor = blankReceiptReminderEditor()" in reset

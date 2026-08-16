from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
PRINT_PAGE = ROOT / "static" / "requisition-production-print.html"


def _run_node(tmp_path: Path, name: str, body: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-65C frontend tests"
    target = tmp_path / name
    target.write_text(
        f"""
const fs=require("fs"),vm=require("vm");
const html=fs.readFileSync(process.argv[2],"utf8");
const scripts=[...html.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/g)].map(x=>x[1]).filter(Boolean);
if(scripts.length!==1)throw new Error(`expected one inline script, got ${{scripts.length}}`);
const sandbox={{
  axios:{{defaults:{{}},interceptors:{{response:{{use(){{}}}}}}}},
  Vue:{{createApp(definition){{sandbox.definition=definition;return{{component(){{return this}},mount(){{return this}}}}}}}},
  localStorage:{{getItem(){{return""}},setItem(){{}},removeItem(){{}}}},
  window:{{location:{{search:"",pathname:"/"}},history:{{replaceState(){{}}}},addEventListener(){{}},removeEventListener(){{}}}},
  document:{{getElementById(){{return null}},querySelector(){{return null}},addEventListener(){{}},removeEventListener(){{}},body:{{classList:{{add(){{}},remove(){{}}}}}}}},
  navigator:{{}},console,URLSearchParams,AbortController,setTimeout,clearTimeout,confirm:()=>true,
}};
vm.createContext(sandbox);vm.runInContext(scripts[0],sandbox);
const methods=sandbox.definition.methods;
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
{body}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(target), str(INDEX)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_order_and_production_dom_are_internal_and_save_is_acknowledgement_gated() -> None:
    source = INDEX.read_text(encoding="utf-8")
    order_panel = source[
        source.index('aria-label="内部生产提醒"') :
        source.index("order-entry-actions", source.index('aria-label="内部生产提醒"'))
    ]
    assert "客户回单交代" in order_panel
    assert "知道了，继续保存" in order_panel
    assert "手工尺寸只匹配客户级提醒" in order_panel
    assert "不会写入订单备注、客户单据或自动修改数量" in order_panel
    assert "orderReminderMatchesCurrentProducts" in order_panel
    assert "orderReminderBlocking()" in source
    assert "/api/orders/fulfillment-reminders" in source
    assert 'v-if="row.fulfillment_reminders?.length"' in source
    assert "reminder in row.fulfillment_reminders" in source


def test_one_customer_request_formal_product_local_match_and_ack_only(tmp_path: Path) -> None:
    body = r"""
let gets=[];
sandbox.axios.get=async(url,options)=>{gets.push({url,options});return {data:{items:[
  {id:1,scope_type:"customer",reminder_type:"production_attention",content:"开机核对",status:"active"},
  {id:2,scope_type:"product",product_id:88,product_code:"BX-88",product_name:"五层箱",reminder_type:"production_attention",content:"首件确认",status:"active"},
],total:2}}};
const app={...methods,user:{id:9},authGeneration:0,modal:{type:"order"},orderForm:{customer_id:7,items:[{manual_size_entry:true,product_id:null,quantity:5}]},orderReminderState:{loading:false,loaded:false,error:"",items:[],total:0,customerId:null,visible:false,acknowledgedCustomerId:null,acknowledgedSignature:""},showToast(){},errorMessage(e){return e.message||"error"},resetPagePerformanceState(){},resetModalA11ySession(){}};
(async()=>{
  assert(await app.loadOrderRemindersForCustomer(7)===true,"order reminder load failed");
  assert(gets.length===1 && gets[0].url==="/api/orders/fulfillment-reminders","customer did not use one batched request");
  const customer=app.orderReminderState.items[0],product=app.orderReminderState.items[1];
  assert(app.orderReminderMatchesCurrentProducts(customer),"customer reminder did not match manual line");
  assert(!app.orderReminderMatchesCurrentProducts(product),"manual product matched formal product reminder");
  assert(app.orderReminderDisplayRows().length===1,"unmatched product reminder was displayed");
  assert(app.acknowledgeOrderReminders()===true && !app.orderReminderState.visible,"customer-only acknowledgement failed");
  app.orderForm.items.push({manual_size_entry:false,product_id:88,quantity:3});
  app.syncOrderReminderVisibility();
  assert(app.orderReminderMatchesCurrentProducts(product) && app.orderReminderDisplayRows().length===2,"formal product did not match locally");
  assert(app.orderReminderState.visible && app.orderReminderBlocking(),"newly matching product reminder did not reopen the save gate");
  app.orderForm.items[1].quantity=9;
  assert(gets.length===1,"line/product/quantity change issued another reminder request");
  const before=JSON.stringify(app.orderForm);
  assert(app.acknowledgeOrderReminders()===true && !app.orderReminderState.visible,"ack did not close summary");
  assert(JSON.stringify(app.orderForm)===before,"acknowledgement mutated or saved order draft");
  assert(app.reopenOrderReminderSummary()===true && app.orderReminderState.visible,"reopen failed");
})().catch(error=>{console.error(error);process.exit(1)});
"""
    _run_node(tmp_path, "p1-65c-order-reminder.js", body)


def test_latest_order_customer_wins_and_retry_keeps_last_good(tmp_path: Path) -> None:
    body = r"""
const pending=[];
sandbox.axios.get=(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));
const app={...methods,user:{id:9},authGeneration:0,modal:{type:"order"},orderForm:{customer_id:7,items:[]},orderReminderState:{loading:false,loaded:false,error:"",items:[],total:0,customerId:null,visible:false,acknowledgedCustomerId:null,acknowledgedSignature:""},showToast(){},errorMessage(e){return e.message||"error"},resetPagePerformanceState(){},resetModalA11ySession(){}};
(async()=>{
  const oldRequest=app.loadOrderRemindersForCustomer(7);
  app.orderForm.customer_id=8;
  const newRequest=app.loadOrderRemindersForCustomer(8);
  pending[1].resolve({data:{items:[{id:8,scope_type:"customer",content:"B客户"}],total:1}});
  pending[0].resolve({data:{items:[{id:7,scope_type:"customer",content:"A客户旧响应"}],total:1}});
  await Promise.all([oldRequest,newRequest]);
  assert(app.orderReminderState.customerId===8 && app.orderReminderState.items[0].content==="B客户","old response overwrote latest customer");
  const retry=app.loadOrderRemindersForCustomer(8);
  pending[2].reject(new Error("temporary outage"));
  assert(await retry===false,"failed retry reported success");
  assert(app.orderReminderState.items[0].content==="B客户","last-good rows were cleared");
})().catch(error=>{console.error(error);process.exit(1)});
"""
    _run_node(tmp_path, "p1-65c-latest.js", body)


def test_pdf_drafts_share_one_customer_request_and_require_one_ack_each(tmp_path: Path) -> None:
    body = r"""
let gets=0;
sandbox.axios.get=async(url,options)=>{gets+=1;return {data:{items:[
  {id:1,scope_type:"customer",content:"客户级"},
  {id:2,scope_type:"product",product_id:88,product_code:"BX-88",content:"产品级"},
],total:2}}};
const app={...methods,user:{id:9},authGeneration:0,modal:{type:"orderPdfImport"},orderImportReminderCache:{},orderImportDrafts:[],showToast(){},errorMessage(e){return e.message||"error"},resetPagePerformanceState(){},resetModalA11ySession(){}};
const first=app.prepareImportDraftReminderState({matched_customer_id:7,items:[{matched_product_id:88,is_new_product:false}]});
const second=app.prepareImportDraftReminderState({matched_customer_id:7,items:[{matched_product_id:null,is_new_product:true}]});
app.orderImportDrafts=[first,second];
(async()=>{
  await app.loadOrderImportReminders();
  assert(gets===1,"two drafts for one customer issued more than one reminder request");
  assert(first._reminder_visible && second._reminder_visible,"summary did not open for each draft");
  assert(app.importDraftReminderMatchesProduct(first,first._reminder_items[1]),"formal PDF product did not match");
  assert(!app.importDraftReminderMatchesProduct(second,second._reminder_items[1]),"new/temporary PDF product matched product reminder");
  assert(app.orderImportReminderDisplayRows(first).length===2 && app.orderImportReminderDisplayRows(second).length===1,"PDF drafts displayed reminders for unrelated products");
  assert(app.acknowledgeImportDraftReminders(first)===true && !first._reminder_visible,"first acknowledgement failed");
  assert(second._reminder_visible,"acknowledging one draft acknowledged a second draft");
  assert(app.acknowledgeImportDraftReminders(second)===true && !second._reminder_visible,"second acknowledgement failed");
  second.items[0].is_new_product=false;second.items[0].matched_product_id=88;
  app.syncOrderImportReminderVisibility(second);
  assert(second._reminder_visible,"newly matching PDF product reminder did not reopen");
  await app.loadOrderImportReminderForDraft(first);
  assert(gets===1,"cached customer reminders re-requested per draft");
})().catch(error=>{console.error(error);process.exit(1)});
"""
    _run_node(tmp_path, "p1-65c-pdf.js", body)


def test_internal_print_shows_reminders_but_customer_safe_hides_them() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    assert "function fulfillmentReminderHtml(card)" in source
    assert "客户回单交代（仅内部）" in source
    assert '<section class="printing-block internal-only">' in source
    assert "body.customer-safe .internal-only { display:none !important; }" in source
    assert "${fulfillmentReminderHtml(card)}" in source
    assert "row.content" in source
    assert "current product" not in source.lower()


def test_logout_and_modal_close_clear_order_reminder_text_and_drafts() -> None:
    source = INDEX.read_text(encoding="utf-8")
    reset = source[source.index("resetPagePerformanceState() {") : source.index("pageCacheFresh(page) {", source.index("resetPagePerformanceState() {"))]
    assert 'this.orderReminderState = {loading:false,loaded:false,error:"",items:[]' in reset
    assert "this.orderImportDrafts = [];" in reset
    assert "this.orderImportReminderCache = {};" in reset
    close = source[source.index("closeModal() {") : source.index("handleMasterSaveRefreshFailure", source.index("closeModal() {"))]
    assert "this.resetOrderReminderState();" in close
    assert 'this.modal?.type === "orderPdfImport"' in close

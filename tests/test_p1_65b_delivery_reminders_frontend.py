from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"


def _run_node(tmp_path: Path, name: str, body: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-65B frontend tests"
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


def test_delivery_reminder_dom_is_internal_batched_and_never_customer_printed() -> None:
    source = INDEX.read_text(encoding="utf-8")
    panel = source[
        source.index('aria-label="内部送货提醒"') :
        source.index('<div v-if="deliveryForm.editingId"', source.index('aria-label="内部送货提醒"'))
    ]
    assert "只供厂内核对，不会写入送货备注或客户打印" in panel
    assert 'role="dialog"' in panel
    assert "知道了，继续开单" in panel
    assert "重新查看" in panel
    assert "本次已处理" in panel
    assert "本单已选商品匹配" in panel
    assert "建议数量" in panel
    assert "v-for=\"row in deliveryReminderState.items\"" in panel
    assert "/api/deliveries/fulfillment-reminders" in source
    assert "/api/finance/fulfillment-reminders/${reminderId}/resolve" in source

    for path in (ROOT / "static").glob("*.html"):
        if path.name == "index.html":
            continue
        text = path.read_text(encoding="utf-8")
        assert "内部送货提醒" not in text, path.name
        assert "fulfillment-reminders" not in text, path.name


def test_one_customer_request_local_matching_and_acknowledgement(tmp_path: Path) -> None:
    body = r"""
let gets=[];
sandbox.axios.get=async(url,options)=>{gets.push({url,options});return {data:{items:[
  {id:1,scope_type:"customer",reminder_type:"delivery_attention",content:"联系仓库",cadence:"continuous",status:"active",version:1},
  {id:2,scope_type:"product",product_id:88,product_code:"BX-88",product_name:"五层箱",reminder_type:"replenishment",content:"补2只",suggested_quantity:"2.000",cadence:"one_time",status:"active",version:1},
],total:2,page:1,page_size:100}}};
const app={...methods,user:{id:9},authGeneration:0,modal:{type:"delivery"},deliveryForm:{editingId:null,customer_id:7,source_tab:"order",source_mode:"order",lines:[]},deliveryReminderState:{loading:false,error:"",items:[],total:0,page:1,pageSize:100,customerId:null,visible:false,acknowledgedCustomerId:null,actionId:null,mutationAttempt:null},showToast(){},errorMessage(e){return e.message||"error"},resetPagePerformanceState(){},resetModalA11ySession(){}};
(async()=>{
  assert(await app.loadDeliveryRemindersForCustomer(7)===true,"customer reminder load failed");
  assert(gets.length===1 && gets[0].url==="/api/deliveries/fulfillment-reminders","customer selection did not use one batched request");
  assert(app.deliveryReminderState.visible && app.deliveryReminderState.total===2,"summary did not open once");
  const productReminder=app.deliveryReminderState.items[1];
  assert(!app.deliveryReminderMatchesCurrentProducts(productReminder),"unselected product was highlighted");
  app.deliveryForm.lines.push(app.createDeliveryLine({product_id:88,order_item_id:901,delivered_quantity:5}));
  assert(app.deliveryReminderMatchesCurrentProducts(productReminder),"selected exact product was not highlighted locally");
  app.deliveryForm.lines[0].delivered_quantity=3;
  app.removeDeliveryLine(app.deliveryForm.lines[0].key);
  assert(gets.length===1,"line selection/removal/quantity change issued another reminder request");
  assert(app.deliveryForm.lines.length===0,"test line did not remove");
  assert(app.acknowledgeDeliveryReminders()===true && !app.deliveryReminderState.visible,"acknowledgement did not close summary");
  assert(app.deliveryReminderState.items.length===2,"session acknowledgement resolved a durable reminder");
  assert(app.reopenDeliveryReminderSummary()===true && app.deliveryReminderState.visible,"count/reopen entry did not restore summary");
  assert(app.deliveryForm.lines.length===0,"suggested replenishment quantity auto-added a delivery line");
})().catch(error=>{console.error(error);process.exit(1)});
"""
    _run_node(tmp_path, "p1-65b-local-match.js", body)


def test_latest_customer_wins_and_last_good_survives_retryable_failure(tmp_path: Path) -> None:
    body = r"""
const pending=[];
sandbox.axios.get=(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));
const app={...methods,user:{id:9},authGeneration:0,modal:{type:"delivery"},deliveryForm:{editingId:null,customer_id:7,source_tab:"order",lines:[]},deliveryReminderState:{loading:false,error:"",items:[],total:0,page:1,pageSize:100,customerId:null,visible:false,acknowledgedCustomerId:null,actionId:null,mutationAttempt:null},showToast(){},errorMessage(e){return e.message||"error"},resetPagePerformanceState(){},resetModalA11ySession(){}};
(async()=>{
  const oldRequest=app.loadDeliveryRemindersForCustomer(7);
  app.deliveryForm.customer_id=8;
  const newRequest=app.loadDeliveryRemindersForCustomer(8);
  pending[1].resolve({data:{items:[{id:8,scope_type:"customer",content:"B客户"}],total:1}});
  pending[0].resolve({data:{items:[{id:7,scope_type:"customer",content:"A客户旧响应"}],total:1}});
  await Promise.all([oldRequest,newRequest]);
  assert(app.deliveryReminderState.customerId===8 && app.deliveryReminderState.items[0].content==="B客户","old customer response overwrote current customer");
  const refresh=app.loadDeliveryRemindersForCustomer(8);
  pending[2].reject(new Error("temporary outage"));
  assert(await refresh===false,"failed refresh reported success");
  assert(app.deliveryReminderState.items[0].content==="B客户","retryable failure cleared last-good reminder data");
  assert(app.deliveryReminderState.error.includes("temporary outage"),"retryable error was not shown");
})().catch(error=>{console.error(error);process.exit(1)});
"""
    _run_node(tmp_path, "p1-65b-latest-wins.js", body)


def test_one_time_resolution_reuses_existing_finance_cas_and_does_not_touch_draft(tmp_path: Path) -> None:
    body = r"""
let posts=[];
sandbox.axios.post=async(url,payload)=>{posts.push({url,payload});return {data:{id:2,status:"resolved",version:2}}};
const messages=[];
const line={key:"line-1",source_type:"order",product_id:88,order_item_id:901,delivered_quantity:5,remarks:"客户可见备注"};
const app={...methods,user:{id:9},authGeneration:0,canFinance:true,modal:{type:"delivery"},deliveryForm:{editingId:null,customer_id:7,source_tab:"order",lines:[line]},deliveryReminderState:{loading:false,error:"",items:[{id:2,scope_type:"product",product_id:88,cadence:"one_time",status:"active",version:1}],total:1,page:1,pageSize:100,customerId:7,visible:true,acknowledgedCustomerId:null,actionId:null,mutationAttempt:null},showToast(message,danger=false){messages.push({message,danger})},errorMessage(e){return e.message||"error"},resetPagePerformanceState(){},resetModalA11ySession(){}};
(async()=>{
  const before=JSON.stringify(app.deliveryForm.lines);
  assert(await app.resolveDeliveryReminderForThisTime(app.deliveryReminderState.items[0])===true,"one-time resolve failed");
  assert(posts.length===1 && posts[0].url.endsWith("/fulfillment-reminders/2/resolve"),"resolve did not reuse the P1-65A endpoint");
  assert(posts[0].payload.expected_version===1 && posts[0].payload.idempotency_key,"resolve omitted CAS or idempotency");
  assert(app.deliveryReminderState.items.length===0 && app.deliveryReminderState.total===0,"resolved reminder remained in summary");
  assert(JSON.stringify(app.deliveryForm.lines)===before,"resolving reminder modified delivery lines or quantity");
})().catch(error=>{console.error(error);process.exit(1)});
"""
    _run_node(tmp_path, "p1-65b-resolve.js", body)


def test_logout_reset_and_delivery_close_clear_internal_reminder_text() -> None:
    source = INDEX.read_text(encoding="utf-8")
    reset = source[
        source.index("resetPagePerformanceState() {") :
        source.index("pageCacheFresh(page) {", source.index("resetPagePerformanceState() {"))
    ]
    assert "this.deliveryReminderState = {loading:false,error:\"\",items:[]" in reset
    assert "mutationAttempt:null" in reset
    close = source[
        source.index("closeModal() {") :
        source.index("handleMasterSaveRefreshFailure", source.index("closeModal() {"))
    ]
    assert 'this.resetDeliveryReminderState()' in close

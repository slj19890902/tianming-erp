from pathlib import Path
import json
import re
import shutil
import subprocess
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(script):
    result = subprocess.run([shutil.which("node"), "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_js_syntax_and_bom_input_save_lifecycle():
    node(r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
for(const file of ['static/index.html','static/requisition-production-print.html','static/mold-label.html','static/warehouse.html','static/mobile_mold_live.html']) {
 const html=fs.readFileSync(file,'utf8');
 for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  if(!match[1].includes('src=')&&!match[1].includes('application/json')) new vm.Script(match[2],{filename:file});
 }
}
const source=fs.readFileSync('static/index.html','utf8');
function section(start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));}
const quantities=vm.runInNewContext('({' + section('          stockPolicyDraftQuantity(policy)', '          async addStockPolicyDraft(policy)') + '})');
const state={stockPolicyDraftQuantities:{}};
assert.equal(quantities.stockPolicyDraftQuantity.call(state,{id:10,suggested_new_requisition_finished_quantity:1440}),1440);
quantities.setStockPolicyDraftQuantity.call(state,{policy_id:10},1800);
assert.equal(quantities.stockPolicyDraftQuantity.call(state,{id:10,suggested_new_requisition_finished_quantity:0}),1800);
let posts=0,reads=0;
const methods=vm.runInNewContext('({' + section('          async saveStockReplenishmentDraft()', '          beginSupplierSheetCuttingEdit(line)') + '})',{
 axios:{post:async(url,payload)=>{posts++;assert.equal(payload.replenishment_plans[0].finished_quantity,1800);return {data:{id:19,order_number:'SRO-TEST-19',status:'draft'}}}},
 window:{}, console,
});
function saveState(page){return {activePage:page,requisitionWorkspace:'board',pages:{},modal:{type:'stockReplenishment'},stockReplenishmentSaveState:{},stockReplenishmentForm:{source_type:'stock_warning',items:[],replenishment_plans:[{finished_quantity:1800}]},validateStockReplenishmentForm:()=>'',invalidatePageCache:()=>{},isCancelledRequest:error=>error.cancelled,showToast:()=>{},errorMessage:error=>error.message,loadRequisition:async()=>{reads++;return false;}};}
(async()=>{
 const offPage=saveState('inventory');let result=await methods.saveStockReplenishmentDraft.call(offPage);
 assert.equal(result._refresh_failed,false);assert.equal(reads,0);assert.equal(offPage.stockReplenishmentSavedNotice.result.order_number,'SRO-TEST-19');
 await methods.saveStockReplenishmentDraft.call(offPage);assert.equal(posts,1);
 const cancelled=saveState('requisition');cancelled.loadRequisition=async()=>{throw {cancelled:true}};
 result=await methods.saveStockReplenishmentDraft.call(cancelled);assert.equal(result._refresh_failed,false);
 const failed=saveState('requisition');failed.loadRequisition=async()=>{throw Error('GET broken')};
 result=await methods.saveStockReplenishmentDraft.call(failed);assert.equal(result._refresh_failed,true);assert.equal(failed.stockReplenishmentSavedNotice.readError,'GET broken');assert.equal(failed.stockReplenishmentSaveState.committed,true);
})().catch(error=>{console.error(error);process.exitCode=1;});''')


def test_route_uses_frozen_steps_without_liner_or_six_step_loss():
    node(r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/requisition-production-print.html','utf8');
const a=source.indexOf('      function processSteps(card)'),b=source.indexOf('      function processHtml(',a);
const f=vm.runInNewContext('(()=>{'+source.slice(a,b)+';return {processSteps,cuttingFacts}})()', {unique:values=>[...new Set(values)],textList:values=>values.join(' / '),numberText:value=>String(value)});
const long=['分切','印刷','开槽','压线','模切','清废','粘贴','捆扎'];
assert.deepEqual(Array.from(f.processSteps({layout_kind:'liner',production_route:{steps:long.map(label=>({label}))}})),long);
const snapshot={length_parts:2,width_parts:2,mold_count:4,is_die_cut:true,supplier_length_mm:1000,supplier_width_mm:800,theoretical_length_mm:500,theoretical_width_mm:400,yield_per_supplier_sheet:16};
assert.deepEqual(Array.from(f.processSteps({layout_kind:'liner',sheet_cutting_snapshot:snapshot})),['分切','模切','计数','捆扎']);
assert.deepEqual(Array.from(f.processSteps({layout_kind:'die_cut',sheet_cutting_snapshot:{...snapshot,length_parts:1,width_parts:1}})),['模切']);
assert.equal(f.processSteps({layout_kind:'die_cut',sheet_cutting_snapshot:snapshot,material_processing_type:'cut_sheet'}).includes('分切'),false);
assert.match(f.cuttingFacts({sheet_cutting_snapshot:snapshot}),/大纸 1000×800mm → 长2×宽2分切 → 每片500×400mm；4模；每张产出16片/);
''')


def product(code, count, a=2, b=2):
    return SimpleNamespace(id=int(code), product_code=code, default_cutting_mode="一开16", sheet_cutting_settings={"schema_version":2,"whole":{"length_parts":a,"width_parts":b,"mold_count":count,"is_die_cut":True}})


def test_mold_count_has_no_cutting_multiplier_or_legacy_override():
    from app.services.mold_label_content import mold_count_projection
    first=product("1",4)
    assert mold_count_projection([first])["label_mold_count"] == "4模"
    assert mold_count_projection([first,product("2",4,1,2)])["label_mold_count"] == "4模"
    conflict=mold_count_projection([first,product("2",2)])
    assert conflict["label_mold_count"] == "几模待核"
    assert [row["mold_count"] for row in conflict["label_mold_count_facts"]] == [4,2]
    first.sheet_cutting_settings=None
    assert mold_count_projection([first])["label_mold_count"] == "几模待核"


def test_cover_base_count_is_per_bound_component():
    from app.services.mold_label_content import mold_count_projection
    first=product("1",4)
    first.sheet_cutting_settings={"schema_version":2,"cover":first.sheet_cutting_settings["whole"],"base":{"length_parts":1,"width_parts":2,"mold_count":2,"is_die_cut":True}}
    projected=mold_count_projection([first])
    assert projected["label_mold_count"] == "几模待核"
    assert [row["display"] for row in projected["label_mold_count_facts"]] == ["盖4模","底2模"]


def test_mold_layout_freezes_old_catalog_and_new_uses_count():
    from app.services import mold_label_layout as layout
    old=layout._default_layout_v9()
    before=json.dumps(old)
    assert layout._normalize_snapshot_layout(old)["catalog_version"] == "mold-edge-v1"
    assert layout._upgrade_to_current_catalog(old)["catalog_version"] == "mold-count-v1"
    assert json.dumps(old)==before
    node(r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/assets/mold-label-layout.js','utf8');
const start=source.indexOf('  function valueForElement(');
assert(start>=0);
const end=source.indexOf('\n  function ',start+5);
const f=vm.runInNewContext('(()=>{'+source.slice(start,end)+';return valueForElement})()',{isEdgeCatalog:v=>['mold-edge-v1','mold-count-v1'].includes(v),V10_CATALOG_VERSION:'mold-count-v1',V9_CATALOG_VERSION:'mold-edge-v1'});
const row={label_mold_count:'4模',label_display_cutting_mode:'一开二',label_inventory_codes:['P1'],label_customer_name:'联测'};
assert.equal(f(row,'cutting_mode','mold-count-v1'),'4模');
assert.equal(f(row,'cutting_mode','mold-edge-v1'),'一开二');
''')


def test_new_label_projection_ignores_old_manual_cutting_override(mold_app):
    from tests.test_p1_103_mold_label_layout import _complete_named_mold
    from tests.test_mold_tool_workflow import _login
    from app.models.product import Product
    from app.models.mold_tool import MoldTool
    from app.services.mold_label_content import canonical_label_overrides
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    app,factory=mold_app
    mold_id=_complete_named_mold(factory)
    with factory() as db:
        first=db.scalar(select(Product).where(Product.mold_tool_id==mold_id))
        first.sheet_cutting_settings=product("1",4).sheet_cutting_settings
        db.get(MoldTool,mold_id).label_overrides_json=canonical_label_overrides({"cutting_mode":"一开二"})
        db.commit()
    with TestClient(app) as client:
        _login(client,"admin")
        response=client.get(f"/api/warehouse/molds/{mold_id}/label-preview")
        assert response.status_code==200,response.text
        row=response.json()
        assert row["label_mold_count"]=="4模"
        assert row["label_overrides"]["cutting_mode"]=="一开二"
        assert row["label_layout"]["layout"]["catalog_version"]=="mold-count-v1"


from tests.test_mold_tool_workflow import mold_app


def test_eight_steps_and_cutting_facts_fit_half_a4_in_isolated_chrome(tmp_path, monkeypatch):
    import test_p0_34_production_print_capacity as capacity
    chrome=Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    if not chrome.exists(): chrome=Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")
    if not chrome.exists(): pytest.skip("本机无隔离Chrome")
    monkeypatch.setattr(capacity,"_headless_browser",lambda:chrome)
    source=(ROOT/"static/requisition-production-print.html").read_text(encoding="utf-8")
    # Inspect after the real asynchronous render finishes, rather than when
    # the old probe first observes an inserted card before fonts settle.
    source=source.replace("printButton.disabled = !layouts.length || packageData.printable === false;", """printButton.disabled = !layouts.length || packageData.printable === false;
        const checked = pagesHost.querySelector('.task-card:not(.blank)');
        document.body.dataset.finalReady='true';
        document.body.dataset.finalOverflow=String(checked.scrollHeight>checked.clientHeight+1);
        document.body.dataset.routeCount=String(checked.querySelectorAll('.process-route li').length);
        document.body.dataset.finalDisabled=String(printButton.disabled);""")
    page=tmp_path/"source.html";page.write_text(source,encoding="utf-8")
    monkeypatch.setattr(capacity,"PRINT_PAGE",page)
    package=capacity._normal_complex_package()
    card=package["cards"][0]
    card["production_route"]={"steps":[{"code":str(i),"label":label} for i,label in enumerate(["分切","印刷","开槽","压线","模切","清废","粘贴","捆扎"])]}
    card["sheet_cutting_snapshot"]={"supplier_length_mm":1930,"supplier_width_mm":1250,"theoretical_length_mm":965,"theoretical_width_mm":625,"length_parts":2,"width_parts":2,"mold_count":4,"yield_per_supplier_sheet":16,"is_die_cut":True}
    output=capacity._render_package(tmp_path,package)
    assert 'data-final-ready="true"' in output
    assert 'data-final-overflow="false"' in output
    assert 'data-route-count="8"' in output
    assert 'data-final-disabled="false"' in output
    assert "每张产出16片" in output


def test_default_bom_with_incoming_omits_manual_quantity_until_user_edits():
    node(r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/index.html','utf8');
const a=source.indexOf('          stockPolicyDraftQuantity(policy)'),b=source.indexOf('          async addCompatibleStockDraft(',a);
const calls=[];
const methods=vm.runInNewContext('({' + source.slice(a,b) + '})', {
 createIdempotencyKey:()=> 'frozen-key',
 axios:{get:async(url,options)=>{
  calls.push(options.params);
  // Target1800 / assembled360 / incoming720: the authoritative default
  // uses gross1440 and returns net720. Explicit1800 returns net1080.
  const gross=options.params.finished_quantity ?? 1440;
  const net=Math.max(gross-720,0);
  return {data:{draft_ready:true,is_virtual_composite_parent:true,replenishment_plan:{policy_id:10,finished_quantity:gross},items:net ? [{stock_policy_id:10,product_id:3771,quantity:Math.ceil(net*3/4)},{stock_policy_id:10,product_id:3783,quantity:net}] : []}};
 }},console,
});
function state(){return {stockPolicyDraftQuantities:{},stockReplenishmentForm:{items:[]},modal:{type:'stockReplenishment'},beginLatestRequest:()=>({signal:{aborted:false}}),finishLatestRequest:()=>{},isCancelledRequest:()=>false,errorMessage:error=>error.message,showToast:()=>{},loadStockProducts:async()=>{},syncStockReplenishmentSupplier:()=>{},...methods};}
(async()=>{
 const policy={id:10,is_virtual_composite_parent:true,suggested_new_requisition_finished_quantity:720};
 const untouched=state();
 assert.equal(untouched.stockPolicyDraftQuantity(policy),720);
 assert.equal(untouched.stockPolicyDraftQuantity({...policy,suggested_new_requisition_finished_quantity:600}),600);
 assert.equal(Object.keys(untouched.stockPolicyDraftQuantities).length,0);
 await untouched.addStockPolicyDraft(policy);
 assert.equal(Object.prototype.hasOwnProperty.call(calls[0],'finished_quantity'),false);
 assert.deepEqual(Array.from(untouched.stockReplenishmentForm.items,line=>line.quantity),[540,720]);
 assert.equal(untouched.stockReplenishmentForm.replenishment_plans[0].finished_quantity,1440);
 assert.equal(Object.keys(untouched.stockPolicyDraftQuantities).length,0);
 const edited=state();edited.setStockPolicyDraftQuantity(policy,'1800');
 await edited.addStockPolicyDraft(policy);
 assert.equal(calls[1].finished_quantity,1800);
 assert.deepEqual(Array.from(edited.stockReplenishmentForm.items,line=>line.quantity),[810,1080]);
 const sameAsSuggested=state();sameAsSuggested.setStockPolicyDraftQuantity(policy,'720');
 await sameAsSuggested.addStockPolicyDraft(policy);
 assert.equal(calls[2].finished_quantity,720);
})().catch(error=>{console.error(error);process.exitCode=1;});''')

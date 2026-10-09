"""Static/low-cost frontend contract checks for N035 stocktake."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_stocktake.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _inline_scripts(source: str) -> str:
    return "\n".join(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL))


def _assert_node_syntax(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    if node is None:
        return
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _function_line(source: str, name: str) -> str:
    prefix = f"function {name}("
    for line in _inline_scripts(source).splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix) or stripped.startswith(f"async {prefix}"):
            return stripped
    raise AssertionError(f"missing frontend function: {name}")


def _assert_node_run(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    if node is None:
        return
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_mobile_stocktake_uses_stocktake_api_and_never_direct_adjust() -> None:
    recovery_path = "static/ui/mobile-stocktake-recovery.js"
    assert f'src="/{recovery_path}?' in MOBILE
    mobile_sources = MOBILE + (ROOT / recovery_path).read_text(encoding="utf-8")
    for endpoint in (
        "/api/auth/login",
        "/api/warehouse/stocktake/locations",
        "/api/warehouse/stocktake/locations/",
        "/api/warehouse/stocktakes",
    ):
        assert endpoint in mobile_sources
    assert "/adjust" not in mobile_sources
    assert "inventory_lot_id" in MOBILE
    assert "client_line_id" in MOBILE
    assert "idempotency_key" in MOBILE
    assert "expected_version" in MOBILE
    assert "expected_available" in MOBILE
    assert "expected_reserved" in MOBILE
    assert "location_address_version" in MOBILE
    assert "location_position_status" in MOBILE
    assert "published_map_revision" in MOBILE


def test_mobile_stocktake_renders_all_lots_and_requires_every_count(tmp_path: Path) -> None:
    assert 'listItems(data,["items","lots","stocktake_items","inventory_lots"])' in MOBILE
    assert "quantity_available" in MOBILE and "quantity_reserved" in MOBILE
    assert 'step="1" inputmode="numeric"' in MOBILE
    assert "全部与系统一致" in MOBILE
    assert "function allCounted()" in MOBILE
    assert "Number.isInteger(value)" in MOBILE
    assert "请先填完全部批次的实盘数量" in MOBILE
    assert "非负整数" in MOBILE
    submit = _function_line(MOBILE, "submitStocktake")
    assert "window.confirm" not in submit
    assert "resetLockedInputs" in MOBILE
    # Execute the actual page and its recovery component: submitting may be
    # controlled by onChange and receipt callbacks rather than this one line.
    node = shutil.which("node")
    assert node is not None, "Node is required for the mobile stocktake behavior contract"
    evidence = tmp_path / "stocktake-recovery.json"
    environment = os.environ.copy()
    environment.pop("TM_STOCKTAKE_BASELINE", None)
    environment["TM_STOCKTAKE_EVIDENCE"] = str(evidence)
    result = subprocess.run(
        [node, str(ROOT / "tests/mobile_stocktake_recovery.cjs")],
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(evidence.read_text(encoding="utf-8"))
    assert not observed["failures"]
    assert "pending double click sends one immutable body" in observed["cases"]
    assert "true cold init selectedLocation null immediately freezes recovered original inputs" in observed["cases"]


def test_warehouse_stocktake_tab_and_review_permissions_are_gated() -> None:
    assert 'data-tab="stocktake_review"' in WAREHOUSE
    assert "warehouse.stocktake.review" in WAREHOUSE
    assert "warehouse.stocktake.view" in WAREHOUSE
    assert "canReviewStocktakes()" in WAREHOUSE
    assert "canViewStocktakes()" in WAREHOUSE
    assert 'id="stocktakeStatusFilter"' in WAREHOUSE
    assert 'id="stocktakeDifferenceFilter"' in WAREHOUSE
    assert 'value="difference">有差异' in WAREHOUSE
    assert 'value="same">无差异' in WAREHOUSE
    assert 'api("/api/warehouse/stocktakes")' in WAREHOUSE
    assert "filteredStocktakeReviews" in WAREHOUSE
    assert "/api/warehouse/stocktakes/" in WAREHOUSE


def test_mobile_reentry_keeps_submitted_location_locked() -> None:
    assert "pending_stocktake" in MOBILE
    assert "STOCKTAKE_PENDING_REVIEW" in MOBILE
    assert "该库位盘点已提交，正在等待电脑端审核" in MOBILE
    assert "resetLockedInputs()" in MOBILE
    assert "state.locked=Boolean(pending)" in MOBILE


def test_warehouse_reject_uses_one_confirmation_and_handles_drift_as_full_restart() -> None:
    method = WAREHOUSE.split("async function rejectStocktake(id)", 1)[1].split(
        "floor3InitAreaFilter()", 1
    )[0]
    assert method.count("confirm(") == 1
    assert "prompt(" not in method
    assert "reason:" not in method
    assert "本操作不会修改库存" in method
    assert "请再次确认：审核通过后将由后端统一处理盘点差异" in WAREHOUSE
    assert "库存已变化，请重新发起盘点" in WAREHOUSE
    assert "closeStocktakeReview();loadStocktakeReviews()" in WAREHOUSE
    assert "stocktake-diff" in WAREHOUSE
    assert "difference_quantity" in WAREHOUSE
    assert "stocktakeSigned(diff)" in WAREHOUSE
    assert "stocktakeSigned(totals.difference)" in WAREHOUSE
    assert "row?.difference??" not in WAREHOUSE


def test_stocktake_errors_use_stable_code_instead_of_all_409_behavior(tmp_path: Path) -> None:
    functions = "\n".join(
        _function_line(MOBILE, name)
        for name in ("apiErrorCode", "errorText", "api", "isStocktakeDrift")
    )
    harness = f"""
{functions}
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
const responses=[
  {{detail:{{code:"OTHER_CONFLICT",message:"其他中文冲突"}}}},
  {{detail:{{code:"STOCKTAKE_DRIFT",message:"库存快照已变化"}}}},
];
global.fetch=async()=>({{ok:false,status:409,json:async()=>responses.shift()}});
(async()=>{{
  try{{await api("/first")}}catch(error){{
    expect(error.code==="OTHER_CONFLICT","non-drift code must be preserved");
    expect(error.message==="其他中文冲突","backend Chinese detail must be shown");
    expect(!isStocktakeDrift(error),"ordinary HTTP 409 must not be treated as drift");
  }}
  try{{await api("/second")}}catch(error){{
    expect(error.code==="STOCKTAKE_DRIFT","drift code must be preserved");
    expect(isStocktakeDrift(error),"stable drift code must trigger drift handling");
  }}
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _assert_node_run(harness, tmp_path, "n035-error-code-behavior.js")
    assert "error.status===409" not in MOBILE
    assert "function stocktakeDrift(error){if(!isStocktakeDrift(error))" in WAREHOUSE


def test_mobile_snapshot_submit_and_retry_idempotency_behavior(tmp_path: Path) -> None:
    functions = "\n".join(
        _function_line(MOBILE, name)
        for name in (
            "resetMobileSubmitAttempt",
            "mobileSubmitIdempotencyKey",
            "lotId",
            "captureLotSnapshots",
            "lotSnapshot",
            "isStocktakeDrift",
            "isStocktakePending",
            "submitStocktake",
        )
    )
    harness = f"""
const state={{selectedLocation:{{id:3,layout_version:1,address_version:2,position_status:"mapped",published_map_revision:"map-rev-1"}},locked:false,submitting:false,clientLineIds:{{7:"line-7"}},lotSnapshots:{{}},submitIdempotencyKey:null}};
const pick=(row,keys,fallback=null)=>{{for(const key of keys){{if(row&&row[key]!==undefined&&row[key]!==null)return row[key]}}return fallback}};
let keyCounter=0;
function idempotencyKey(){{keyCounter+=1;return `key-${{keyCounter}}`}}
const nodes={{submitButton:{{textContent:"",disabled:false}},stocktakeNumber:{{textContent:""}},lockedResult:{{classList:{{remove:()=>{{}}}}}}}};
const $=id=>nodes[id];
const input={{dataset:{{lotId:"7"}},value:"12",disabled:false}};
const document={{querySelectorAll:selector=>selector===".count-input"?[input]:[]}};
const window={{confirm:()=>true}};
function allCounted(){{return true}}
function updateSubmitState(){{}}
function showMessage(){{}}
function handleStocktakeAuth(){{return false}}
function resetLockedInputs(){{state.locked=true}}
function resetToLocations(){{throw new Error("non-drift error must not clear the draft")}}
const requests=[];
let succeed=false;
async function api(url,options){{requests.push(JSON.parse(options.body));if(!succeed){{const error=new Error("请重试");error.code="OTHER_CONFLICT";throw error}}return {{stocktake_number:"ST-1"}}}}
{functions}
(async()=>{{
  captureLotSnapshots([{{id:7,version:4,quantity_available:10,quantity_reserved:2}}]);
  await submitStocktake();
  await submitStocktake();
  expect(requests[0].idempotency_key===requests[1].idempotency_key,"unchanged retry must reuse mobile idempotency key");
  const firstItem=requests[0].items[0];
  expect(firstItem.expected_version===4,"version snapshot must be submitted");
  expect(firstItem.expected_available===10,"available snapshot must be submitted");
  expect(firstItem.expected_reserved===2,"reserved snapshot must be submitted");
  expect(requests[0].location_address_version===2,"address version must be submitted");
  expect(requests[0].location_position_status==="mapped","position status must be submitted");
  expect(requests[0].published_map_revision==="map-rev-1","map revision must be submitted");
  resetMobileSubmitAttempt();
  await submitStocktake();
  expect(requests[2].idempotency_key!==requests[1].idempotency_key,"content change reset must create a new key");
  succeed=true;
  await submitStocktake();
  expect(requests[3].idempotency_key===requests[2].idempotency_key,"unchanged retry before success must reuse key");
  expect(state.submitIdempotencyKey===null,"success must reset mobile attempt key");
}})().catch(error=>{{console.error(error);process.exit(1)}});
function expect(condition,message){{if(!condition)throw new Error(message)}}
"""
    _assert_node_run(harness, tmp_path, "n035-mobile-submit-behavior.js")


def test_warehouse_review_idempotency_signature_behavior(tmp_path: Path) -> None:
    functions = "\n".join(
        _function_line(WAREHOUSE, name)
        for name in (
            "resetStocktakeReviewAttempt",
            "selectStocktakeReview",
            "stocktakeReviewIdempotencyKey",
        )
    )
    harness = f"""
const state={{stocktakeReviewSelectedId:null,stocktakeReviewAttempt:{{signature:"",key:null}}}};
let keyCounter=0;
function createIdempotencyKey(){{keyCounter+=1;return `review-${{keyCounter}}`}}
{functions}
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
const first=stocktakeReviewIdempotencyKey(10,"reject","数量有误");
expect(stocktakeReviewIdempotencyKey(10,"reject","数量有误")===first,"same review action and reason must reuse key");
const changedReason=stocktakeReviewIdempotencyKey(10,"reject","库位不符");
expect(changedReason!==first,"changed reject reason must reset key");
const changedAction=stocktakeReviewIdempotencyKey(10,"approve","");
expect(changedAction!==changedReason,"changed action must reset key");
const switched=stocktakeReviewIdempotencyKey(11,"approve","");
expect(switched!==changedAction,"switching stocktake must reset key");
resetStocktakeReviewAttempt();
expect(stocktakeReviewIdempotencyKey(11,"approve","")!==switched,"success reset must create a new key");
"""
    _assert_node_run(harness, tmp_path, "n035-review-idempotency-behavior.js")


def test_warehouse_stocktake_tab_default_visibility_behavior(tmp_path: Path) -> None:
    assert '<section id="stocktakeReviewSection" class="hidden">' in WAREHOUSE
    assert "document.querySelectorAll(\".stocktake-review-access\")" not in WAREHOUSE
    functions = "\n".join(
        _function_line(WAREHOUSE, name)
        for name in ("revealStocktakeReviewTab", "setStocktakeReviewSectionVisibility")
    )
    harness = f"""
function classList(initial){{const values=new Set(initial);return {{contains:value=>values.has(value),remove:value=>values.delete(value),toggle:(value,force)=>force?values.add(value):values.delete(value)}}}}
const nodes={{stocktakeReviewTab:{{classList:classList(["hidden"])}},stocktakeReviewSection:{{classList:classList(["hidden"])}}}};
const $=id=>nodes[id];
const canViewStocktakes=()=>true;
{functions}
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
revealStocktakeReviewTab();
expect(!nodes.stocktakeReviewTab.classList.contains("hidden"),"authorized review tab must be visible");
expect(nodes.stocktakeReviewSection.classList.contains("hidden"),"review content must remain hidden during initialization");
setStocktakeReviewSectionVisibility("finished");
expect(nodes.stocktakeReviewSection.classList.contains("hidden"),"finished tab must keep review content hidden");
setStocktakeReviewSectionVisibility("stocktake_review");
expect(!nodes.stocktakeReviewSection.classList.contains("hidden"),"review tab must reveal review content");
"""
    _assert_node_run(harness, tmp_path, "n035-review-visibility-behavior.js")


def test_mobile_specification_snapshot_render_behavior(tmp_path: Path) -> None:
    for field in ("specification_snapshot", "specification", '"规格"'):
        assert field in MOBILE
    functions = "\n".join(
        _function_line(MOBILE, name)
        for name in ("resetMobileSubmitAttempt", "lotId", "systemQuantity", "lotNumber", "renderLots")
    )
    harness = f"""
const state={{lots:[{{id:7,inventory_code:"P-7",product_name:"纸箱",customer_name:"客户",lot_number:"L-7",quantity_available:10,quantity_reserved:2,specification_snapshot:"500×300×200",specification:"旧规格"}}],clientLineIds:{{}},locked:false,submitIdempotencyKey:"existing-key"}};
const pick=(row,keys,fallback=null)=>{{for(const key of keys){{if(row&&row[key]!==undefined&&row[key]!==null)return row[key]}}return fallback}};
const numberValue=(row,keys)=>{{const value=Number(pick(row,keys,0));return Number.isFinite(value)?value:0}};
const h=value=>String(value??"");
const classList={{add:()=>{{}},toggle:()=>{{}}}};
const nodes={{lotSummary:{{innerHTML:""}},lotList:{{innerHTML:""}},submitArea:{{classList}}}};
const $=id=>nodes[id];
const renderedInput={{oninput:null}};
const document={{querySelectorAll:selector=>selector===".count-input"?[renderedInput]:[]}};
const CSS={{escape:value=>value}};
function updateSameHint(){{}}
function updateSubmitState(){{}}
{functions}
renderLots();
if(!nodes.lotList.innerHTML.includes("规格：500×300×200"))throw new Error("specification snapshot must be rendered");
if(nodes.lotList.innerHTML.includes("规格：旧规格"))throw new Error("snapshot specification must have priority");
renderedInput.oninput();
if(state.submitIdempotencyKey!==null)throw new Error("editing counted content must reset mobile attempt key");
"""
    _assert_node_run(harness, tmp_path, "n035-mobile-specification-behavior.js")


def test_warehouse_stocktake_snapshot_field_contract_behavior(tmp_path: Path) -> None:
    for field in (
        "snapshot_on_hand",
        "quantity_on_hand_snapshot",
        "quantity_available_snapshot",
        "quantity_reserved_snapshot",
        "counted_quantity",
        "difference_quantity",
    ):
        assert field in WAREHOUSE

    functions = "\n".join(
        _function_line(WAREHOUSE, name)
        for name in (
            "stocktakeDetailRows",
            "stocktakeOptionalNumber",
            "stocktakeNumberValue",
            "stocktakeSystem",
            "stocktakeCounted",
            "stocktakeDiff",
            "stocktakeTotals",
        )
    )
    harness = f"""
{functions}
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
const payload={{
  snapshot_on_hand:15,
  counted_quantity:13,
  difference_quantity:-2,
  items:[{{
    quantity_on_hand_snapshot:15,
    quantity_available_snapshot:12,
    quantity_reserved_snapshot:3,
    counted_quantity:13,
    difference_quantity:-2,
  }}],
}};
const totals=stocktakeTotals(payload);
expect(totals.system===15,"top-level snapshot_on_hand must drive system total");
expect(totals.counted===13,"top-level counted_quantity must drive counted total");
expect(totals.difference===-2,"top-level signed difference_quantity must be preserved");
expect(stocktakeSystem(payload.items[0])===15,"item quantity_on_hand_snapshot must drive system quantity");
expect(stocktakeSystem({{quantity_available_snapshot:8,quantity_reserved_snapshot:2}})===10,"snapshot parts must sum when on-hand snapshot is absent");
expect(stocktakeSystem({{quantity_available:4,quantity_reserved:1}})===5,"legacy quantity fields must remain compatible");
expect(stocktakeSystem({{quantity_on_hand_snapshot:0,quantity_available:99}})===0,"zero on-hand snapshot must not fall through");
const zeroTotals=stocktakeTotals({{snapshot_on_hand:0,counted_quantity:0,difference_quantity:0,items:[{{quantity_on_hand_snapshot:9,counted_quantity:9,difference_quantity:0}}]}});
expect(zeroTotals.system===0&&zeroTotals.counted===0&&zeroTotals.difference===0,"zero top-level snapshot values must remain authoritative");
"""
    _assert_node_run(harness, tmp_path, "n035-warehouse-snapshot-contract.js")


def test_f12_f34_are_explicitly_aisle_temporary_locations() -> None:
    assert 'code==="F12"||code==="F34"' in MOBILE
    assert "location?.is_temporary" in MOBILE
    assert 'areaCode==="F12"||areaCode==="F34"' in MOBILE
    assert 'code.startsWith("F12-")||code.startsWith("F34-")' in MOBILE
    assert "过道临放" in MOBILE
    assert "F12 / F34 过道临放" in WAREHOUSE


def test_mobile_location_list_requires_area_or_search_before_rendering() -> None:
    assert "if(!area&&!query)" in MOBILE
    assert "请先选择区域或输入中文位置名称" in MOBILE
    assert '<option value="">请选择区域</option>' in MOBILE


def test_mobile_location_loading_empty_error_and_retry_are_explicit() -> None:
    assert 'id="locationLoadState"' in MOBILE
    assert 'id="retryLocations"' in MOBILE
    assert "正在读取楼层、区域和库位" in MOBILE
    assert "当前没有可盘点库位" in MOBILE
    assert "重新读取" in MOBILE
    assert "重新加载当前库位" in MOBILE
    assert "lot_count" in MOBILE
    assert "current_on_hand" in MOBILE
    load_locations = _function_line(MOBILE, "loadLocations")
    assert "setLocationLoading(true" in load_locations
    assert "setLocationLoading(false" in load_locations
    assert "state.locations=[]" in load_locations
    assert "renderLocationLoadError" in _function_line(MOBILE, "openLocation")


def test_mobile_location_filter_and_temporary_detection_behavior(tmp_path: Path) -> None:
    functions = "\n".join(
        _function_line(MOBILE, name)
        for name in ("isTemporary", "locationName", "locationArea", "renderLocations")
    )
    harness = f"""
const nodes={{
  areaFilter:{{value:""}},
  locationSearch:{{value:""}},
  locationList:{{innerHTML:""}},
}};
const $=id=>nodes[id];
const h=value=>String(value??"");
const pick=(row,keys,fallback=null)=>{{for(const key of keys){{if(row&&row[key]!==undefined&&row[key]!==null)return row[key]}}return fallback}};
const numberValue=(row,keys)=>{{const value=Number(pick(row,keys,0));return Number.isFinite(value)?value:0}};
const state={{locations:[
  {{id:1,area_code:"F12",area_name:"过道临放区",location_code:"F12-P01",location_name:"临放一号",employee_location_name:"三楼过道临放一号"}},
  {{id:2,area_code:"A1",area_name:"A1区",location_code:"A1-P01",location_name:"正常库位",employee_location_name:"三楼 A1区·第1排·1号位"}},
]}};
const document={{querySelectorAll:()=>[]}};
function openLocation(){{}}
{functions}
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
expect(isTemporary({{location_code:"F12-P01"}}),"F12 prefix must be temporary");
expect(isTemporary({{location_code:"F34-P01"}}),"F34 prefix must be temporary");
expect(isTemporary({{area_code:"F12",location_code:"OTHER"}}),"F12 area must be temporary");
expect(isTemporary({{is_temporary:true,location_code:"OTHER"}}),"API temporary flag must win");
expect(!isTemporary({{area_code:"A1",location_code:"A1-P01"}}),"normal location must stay normal");
renderLocations();
expect(nodes.locationList.innerHTML.includes("请先选择区域或输入中文位置名称"),"empty filters need guidance");
expect(!nodes.locationList.innerHTML.includes("location-button"),"empty filters must not render all locations");
nodes.areaFilter.value="过道临放区";
renderLocations();
expect(nodes.locationList.innerHTML.includes("三楼过道临放一号（过道临放）"),"area filter must render a readable F12 location");
nodes.areaFilter.value="";
nodes.locationSearch.value="A1-P01";
renderLocations();
expect(nodes.locationList.innerHTML.includes("三楼 A1区·第1排·1号位"),"location search must render the readable matching location");
"""
    _assert_node_run(harness, tmp_path, "n035-mobile-location-behavior.js")


def test_inline_javascript_is_valid_when_node_is_available(tmp_path: Path) -> None:
    _assert_node_syntax(_inline_scripts(MOBILE), tmp_path, "n035-mobile-stocktake.js")
    _assert_node_syntax(_inline_scripts(WAREHOUSE), tmp_path, "n035-warehouse.js")

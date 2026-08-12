from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _javascript_function(name: str) -> str:
    markers = (f"function {name}(", f"async function {name}(")
    starts = [WAREHOUSE_HTML.find(marker) for marker in markers]
    start = min(index for index in starts if index >= 0)
    open_paren = WAREHOUSE_HTML.find("(", start)
    paren_depth = 0
    close_paren = -1
    for index in range(open_paren, len(WAREHOUSE_HTML)):
        if WAREHOUSE_HTML[index] == "(":
            paren_depth += 1
        elif WAREHOUSE_HTML[index] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = index
                break
    assert close_paren >= 0, f"unterminated JavaScript signature: {name}"
    brace = WAREHOUSE_HTML.find("{", close_paren)
    depth = 0
    quote = ""
    escaped = False
    template_depth = 0
    for index in range(brace, len(WAREHOUSE_HTML)):
        char = WAREHOUSE_HTML[index]
        if escaped:
            escaped = False
            continue
        if quote:
            if char == "\\":
                escaped = True
            elif char == quote and not (quote == "`" and template_depth):
                quote = ""
            elif quote == "`" and char == "$" and WAREHOUSE_HTML[index + 1 : index + 2] == "{":
                template_depth += 1
            elif quote == "`" and char == "}" and template_depth:
                template_depth -= 1
            continue
        if char in ('"', "'", "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return WAREHOUSE_HTML[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_checklist_is_admin_only_and_reuses_existing_area_editor() -> None:
    assert 'id="capacityReviewChecklist" class="capacity-review-checklist admin-only hidden"' in WAREHOUSE_HTML
    assert 'class="btn admin-only" type="button" onclick="openCapacityReviewLedger({focusChecklist:true})">容量复核</button>' in WAREHOUSE_HTML
    assert 'onclick="openCapacityReviewLedger({areaId:${Number(area.id)}})"' in WAREHOUSE_HTML
    assert '$("warehouseAreaCapacityReview").focus()' in WAREHOUSE_HTML
    assert 'editWarehouseArea(area.id)' in WAREHOUSE_HTML
    assert 'capacityReviewSpaceOnly=true;try{await switchLocationView("ledger")}finally{capacityReviewSpaceOnly=false}' in WAREHOUSE_HTML
    helper = _javascript_function("openCapacityReviewLedger")
    assert "/api/" not in helper
    assert "fetch(" not in helper


def test_review_rows_match_p1_27c_confirmation_rule_and_stable_order() -> None:
    script = "\n".join(
        (
            _javascript_function("warehouseCapacityReviewComplete"),
            _javascript_function("warehouseCapacityReviewRows"),
            r"""
const state={warehouseFloors:[
  {id:3,floor_number:3,floor_code:"3F",areas:[
    {id:32,area_code:"B2",construction_status:"enabled",capacity_review_status:"confirmed",capacity_reviewed_at:"2026-08-12T09:00:00"},
    {id:31,area_code:"A1",construction_status:"enabled",capacity_review_status:"excluded",capacity_reviewed_at:null},
  ]},
  {id:1,floor_number:1,floor_code:"1F",areas:[
    {id:12,area_code:"Z9",construction_status:"layout_complete",capacity_review_status:"pending",capacity_reviewed_at:null},
    {id:11,area_code:"A2",construction_status:"enabled",capacity_review_status:"excluded",capacity_reviewed_at:"2026-08-12T08:00:00"},
  ]},
]};
const rows=warehouseCapacityReviewRows();
console.log(JSON.stringify({
  keys:rows.map(row=>`${row.floor.floor_code}/${row.area.area_code}`),
  complete:rows.map(row=>warehouseCapacityReviewComplete(row.area)),
}));
""",
        )
    )
    payload = _run_node(script)
    assert payload == {
        "keys": ["1F/A2", "3F/A1", "3F/B2"],
        "complete": [True, False, True],
    }


def test_open_review_selects_floor_opens_existing_form_and_focuses_review() -> None:
    functions = "\n".join(
        (
            _javascript_function("activateWarehouseLocationHub"),
            _javascript_function("openCapacityReviewLedger"),
        )
    )
    script = functions + r"""
const calls=[];
const fakeClassList={add(){},remove(){}};
const elements=new Map();
for(const id of ["locationViewTabs","inventorySection","insightSection","movementSection","moldSection","printingPlateSection","finishedForm","semiForm","warehouseAreaCapacityReview","capacityReviewChecklist"]){
  elements.set(id,{classList:fakeClassList,scrollIntoView(){calls.push(`scroll:${id}`)},focus(){calls.push(`focus:${id}`)}});
}
const $=id=>elements.get(id);
const document={body:{classList:fakeClassList},querySelectorAll(){return []}};
const state={tab:"finished",warehouseFloors:[{id:9,floor_number:3,areas:[{id:42,floor_id:9,area_code:"B2"}]}]};
const canManageLocations=()=>true;
const toast=message=>calls.push(`toast:${message}`);
const setStocktakeReviewSectionVisibility=tab=>calls.push(`stocktake:${tab}`);
const setInventoryOnboardingSectionVisibility=tab=>calls.push(`onboarding:${tab}`);
let capacityReviewSpaceOnly=false;
const switchLocationView=async view=>calls.push(`view:${view}:${capacityReviewSpaceOnly}`);
const selectWarehouseFloor=id=>calls.push(`floor:${id}`);
const editWarehouseArea=id=>calls.push(`area:${id}`);
const requestAnimationFrame=callback=>callback();
(async()=>{
  const result=await openCapacityReviewLedger({areaId:42});
  console.log(JSON.stringify({result,tab:state.tab,calls}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    payload = _run_node(script)
    assert payload["result"] is True
    assert payload["tab"] == "locations"
    assert "view:ledger:true" in payload["calls"]
    assert "floor:9" in payload["calls"]
    assert "area:42" in payload["calls"]
    assert "focus:warehouseAreaCapacityReview" in payload["calls"]


def test_empty_all_excluded_and_complete_states_are_explicit() -> None:
    render = _javascript_function("renderCapacityReviewChecklist")
    assert "尚无已启用区域" in render
    assert "系统不会把空台账当成容量已确认" in render
    assert 'row.area.capacity_review_status==="confirmed"&&row.area.capacity_eligible&&Number(row.area.confirmed_pallet_capacity)>0' in render
    assert "没有可计入的现场安全容量，精确指标仍不会发布" in render
    assert "现场安全容量复核已全部完成" in render
    assert 'status=incomplete?`${warehouseCapacityReviewLabel(area.capacity_review_status)} · 记录不完整`' in render


def test_render_checklist_distinguishes_empty_zero_capacity_complete_and_pending() -> None:
    functions = "\n".join(
        (
            _javascript_function("warehouseCapacityReviewLabel"),
            _javascript_function("warehouseCapacityReviewComplete"),
            _javascript_function("warehouseCapacityReviewRows"),
            _javascript_function("renderCapacityReviewChecklist"),
        )
    )
    script = functions + r"""
function classes(){const values=new Set(["hidden"]);return {add(...items){items.forEach(item=>values.add(item))},remove(...items){items.forEach(item=>values.delete(item))},values}}
const elements={
  capacityReviewChecklist:{classList:classes()},
  capacityReviewCount:{textContent:""},
  capacityReviewHelp:{textContent:""},
  capacityReviewItems:{innerHTML:""},
};
const $=id=>elements[id];
const h=value=>String(value??"");
const canManageLocations=()=>true;
const state={warehouseFloors:[]};
function snapshot(){return {classes:[...elements.capacityReviewChecklist.classList.values].sort(),count:elements.capacityReviewCount.textContent,help:elements.capacityReviewHelp.textContent,items:elements.capacityReviewItems.innerHTML}}
renderCapacityReviewChecklist();const empty=snapshot();
state.warehouseFloors=[{id:1,floor_number:1,floor_code:"1F",areas:[{id:11,floor_id:1,area_code:"A1",area_name:"通道",construction_status:"enabled",capacity_review_status:"excluded",capacity_reviewed_at:"2026-08-12",capacity_eligible:false,confirmed_pallet_capacity:null,planned_pallet_capacity:5}]}];
renderCapacityReviewChecklist();const zero=snapshot();
state.warehouseFloors[0].areas.push({id:12,floor_id:1,area_code:"A2",area_name:"长期区",construction_status:"enabled",capacity_review_status:"confirmed",capacity_reviewed_at:"2026-08-12",capacity_eligible:true,confirmed_pallet_capacity:20,planned_pallet_capacity:22});
renderCapacityReviewChecklist();const complete=snapshot();
state.warehouseFloors[0].areas.push({id:13,floor_id:1,area_code:"A3",area_name:"待核区",construction_status:"enabled",capacity_review_status:"pending",capacity_reviewed_at:null,capacity_eligible:false,confirmed_pallet_capacity:null,planned_pallet_capacity:8});
renderCapacityReviewChecklist();const pending=snapshot();
console.log(JSON.stringify({empty,zero,complete,pending}));
"""
    payload = _run_node(script)
    assert payload["empty"]["count"] == "尚无已启用区域"
    assert "empty" in payload["zero"]["classes"]
    assert "精确指标仍不会发布" in payload["zero"]["items"]
    assert "complete" in payload["complete"]["classes"]
    assert payload["complete"]["count"] == "已完成 2/2"
    assert payload["pending"]["count"] == "待确认 1 · 已完成 2/3"
    assert "1F · A3 待核区" in payload["pending"]["items"]
    assert "areaId:13" in payload["pending"]["items"]


def test_deep_link_opens_checklist_without_loading_full_location_ledger() -> None:
    deep_link = _javascript_function("applyWarehouseDeepLink")
    switch_view = _javascript_function("switchLocationView")
    assert 'requestedLocationView==="ledger"&&params.get("capacity_review")==="1"' in deep_link
    assert "await openCapacityReviewLedger({focusChecklist:true})" in deep_link
    assert "if(!spaceOnly)await loadLocations(true)" in switch_view
    assert 'if(spaceOnly&&state.warehouseFloors.length)renderWarehouseSpace()' in switch_view

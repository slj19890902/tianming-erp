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


def test_evidence_and_next_action_stay_inside_admin_capacity_workbench() -> None:
    assert 'id="capacityReviewNext" class="btn primary hidden"' in WAREHOUSE_HTML
    assert 'onclick="openNextCapacityReview()">继续复核下一项</button>' in WAREHOUSE_HTML
    assert 'class="capacity-review-checklist admin-only hidden"' in WAREHOUSE_HTML
    assert "capacity_reviewed_by" in _javascript_function("warehouseCapacityReviewEvidenceText")
    assert "capacity_reviewed_at" in _javascript_function("warehouseCapacityReviewEvidenceText")
    helper = _javascript_function("openNextCapacityReview")
    assert "canManageLocations()" in helper
    assert "/api/" not in helper
    assert "fetch(" not in helper


def test_evidence_uses_existing_reviewer_facts_and_never_guesses() -> None:
    script = "\n".join(
        (
            _javascript_function("warehouseCapacityReviewComplete"),
            _javascript_function("warehouseCapacityReviewEvidenceText"),
            r"""
const formatDateTime=value=>`TIME:${value}`;
const complete={capacity_review_status:"confirmed",capacity_reviewed_by:"admin <甲>",capacity_reviewed_at:"2026-08-12T09:30:00"};
const missingActor={capacity_review_status:"excluded",capacity_reviewed_by:null,capacity_reviewed_at:"2026-08-12T10:00:00"};
const incomplete={capacity_review_status:"confirmed",capacity_reviewed_by:"admin",capacity_reviewed_at:null};
console.log(JSON.stringify({
  complete:warehouseCapacityReviewEvidenceText(complete),
  missingActor:warehouseCapacityReviewEvidenceText(missingActor),
  incomplete:warehouseCapacityReviewEvidenceText(incomplete),
}));
""",
        )
    )
    assert _run_node(script) == {
        "complete": "复核：admin <甲> · TIME:2026-08-12T09:30:00",
        "missingActor": "复核：未记录操作人 · TIME:2026-08-12T10:00:00",
        "incomplete": "尚无完整复核记录",
    }


def test_next_review_uses_the_same_stable_order_and_admin_gate() -> None:
    functions = "\n".join(
        (
            _javascript_function("warehouseCapacityReviewComplete"),
            _javascript_function("warehouseCapacityReviewRows"),
            _javascript_function("openNextCapacityReview"),
        )
    )
    script = functions + r"""
const calls=[];
const state={warehouseFloors:[
  {id:3,floor_number:3,floor_code:"3F",areas:[
    {id:32,area_code:"B2",construction_status:"enabled",capacity_review_status:"pending",capacity_reviewed_at:null},
    {id:31,area_code:"A1",construction_status:"enabled",capacity_review_status:"pending",capacity_reviewed_at:null},
  ]},
  {id:1,floor_number:1,floor_code:"1F",areas:[
    {id:11,area_code:"A2",construction_status:"enabled",capacity_review_status:"excluded",capacity_reviewed_at:"2026-08-12"},
  ]},
]};
let admin=true;
const canManageLocations=()=>admin;
const toast=(message,error)=>calls.push(`toast:${message}:${Boolean(error)}`);
const openCapacityReviewLedger=async options=>{calls.push(`open:${options.areaId}`);return true};
(async()=>{
  const first=await openNextCapacityReview();
  state.warehouseFloors[0].areas[1].capacity_review_status="confirmed";
  state.warehouseFloors[0].areas[1].capacity_reviewed_at="2026-08-12";
  const second=await openNextCapacityReview();
  admin=false;
  const denied=await openNextCapacityReview();
  console.log(JSON.stringify({first,second,denied,calls}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    payload = _run_node(script)
    assert payload["first"] is True
    assert payload["second"] is True
    assert payload["denied"] is False
    assert payload["calls"][:2] == ["open:31", "open:32"]
    assert payload["calls"][-1] == "toast:只有管理员可以复核仓库安全容量:true"


def test_completed_summary_shows_latest_existing_reviewer_and_hides_next() -> None:
    functions = "\n".join(
        (
            _javascript_function("warehouseCapacityReviewLabel"),
            _javascript_function("warehouseCapacityReviewComplete"),
            _javascript_function("warehouseCapacityReviewRows"),
            _javascript_function("renderCapacityReviewChecklist"),
        )
    )
    script = functions + r"""
function classes(){const values=new Set(["hidden"]);return {add(...items){items.forEach(item=>values.add(item))},remove(...items){items.forEach(item=>values.delete(item))},toggle(item,force){if(force)values.add(item);else values.delete(item)},values}}
const elements={
  capacityReviewChecklist:{classList:classes()},capacityReviewCount:{textContent:""},capacityReviewHelp:{textContent:""},
  capacityReviewItems:{innerHTML:""},capacityReviewNext:{classList:classes(),disabled:false},
};
const $=id=>elements[id];
const h=value=>String(value??"").replaceAll("<","&lt;");
const formatDateTime=value=>`TIME:${value}`;
const canManageLocations=()=>true;
const state={warehouseFloors:[{id:1,floor_number:1,floor_code:"1F",areas:[
  {id:11,area_code:"A1",construction_status:"enabled",capacity_review_status:"excluded",capacity_reviewed_by:"early",capacity_reviewed_at:"2026-08-12T08:00:00",capacity_eligible:false,confirmed_pallet_capacity:null},
  {id:12,area_code:"A2",construction_status:"enabled",capacity_review_status:"confirmed",capacity_reviewed_by:"admin <末>",capacity_reviewed_at:"2026-08-12T10:00:00",capacity_eligible:true,confirmed_pallet_capacity:20},
]}]};
renderCapacityReviewChecklist();
console.log(JSON.stringify({count:elements.capacityReviewCount.textContent,items:elements.capacityReviewItems.innerHTML,nextHidden:elements.capacityReviewNext.classList.values.has("hidden"),nextDisabled:elements.capacityReviewNext.disabled}));
"""
    payload = _run_node(script)
    assert payload["count"] == "已完成 2/2"
    assert "最后复核：admin &lt;末> · TIME:2026-08-12T10:00:00" in payload["items"]
    assert payload["nextHidden"] is True
    assert payload["nextDisabled"] is True


def test_area_table_escapes_review_evidence_and_save_flow_is_unchanged() -> None:
    render = _javascript_function("renderWarehouseSpace")
    assert "warehouseCapacityReviewEvidenceText(area)" in render
    assert "${h(warehouseCapacityReviewEvidenceText(area))}" in render
    save = _javascript_function("saveWarehouseArea")
    assert "/api/warehouse/space/areas" in save
    assert "await loadWarehouseSpace()" in save
    assert "capacity_reviewed_by" not in save
    assert "capacity_reviewed_at" not in save

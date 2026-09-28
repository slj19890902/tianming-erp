from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LEDGER = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\b", LEDGER)
    assert match, f"missing {name}"
    parentheses = 0
    body_start = None
    for index in range(match.end(), len(LEDGER)):
        if LEDGER[index] == "(":
            parentheses += 1
        elif LEDGER[index] == ")":
            parentheses -= 1
        elif LEDGER[index] == "{" and parentheses == 0:
            body_start = index
            break
    assert body_start is not None, f"missing body for {name}"
    depth = 0
    for index in range(body_start, len(LEDGER)):
        if LEDGER[index] == "{":
            depth += 1
        elif LEDGER[index] == "}":
            depth -= 1
            if depth == 0:
                return LEDGER[match.start() : index + 1]
    raise AssertionError(f"unterminated {name}")

def _run_workspace_functions(*names: str) -> str:
    source = "\n".join(_function_source(name) for name in names)
    program = f"""
const assert = require('node:assert/strict');
global.window = {{location: {{origin: 'https://erp.test'}}}};
global.$ = () => ({{classList: {{contains: () => false}}, closest: () => null}});
{source}
const current = {{query: '已提交', tab: 'finished', searchFloor: 'ALL', locationId: 11, lotId: 22, action: 'move'}};
const uiOnly = warehouseWorkspaceActivationPlan('/warehouse-ledger.html?embedded=1');
assert.equal(warehouseWorkspaceActivationNeedsApply(uiOnly, current), false);
const same = warehouseWorkspaceActivationPlan('/warehouse-ledger.html?tab=finished&q=%E5%B7%B2%E6%8F%90%E4%BA%A4&search_floor=ALL&location_id=11&lot_id=22&action=move');
assert.equal(warehouseWorkspaceActivationNeedsApply(same, current), false);
const clearQuery = warehouseWorkspaceActivationPlan('/warehouse-ledger.html?tab=finished&q=');
assert.equal(clearQuery.hasQuery, true);
assert.equal(clearQuery.query, '');
assert.equal(warehouseWorkspaceActivationNeedsApply(clearQuery, current), true);
const unlocated = warehouseWorkspaceActivationPlan('/warehouse-ledger.html?search_floor=UNLOCATED');
assert.equal(warehouseWorkspaceActivationNeedsApply(unlocated, current), true);
assert.equal(warehouseWorkspaceActivationNeedsApply(unlocated, {{...current, searchFloor: 'UNLOCATED'}}), false);
const semi = warehouseWorkspaceActivationPlan('/warehouse-ledger.html?tab=semi_finished&q=半成品');
assert.equal(warehouseWorkspaceActivationNeedsApply(semi, current), true);
assert.equal(warehouseLedgerInventoryType(null, 'semi_finished', 'finished'), 'semi_finished');
assert.equal(warehouseLedgerInventoryType({{inventory_type: 'finished'}}, 'semi_finished', 'semi_finished'), 'finished');
assert.equal(warehouseWorkspaceShouldRenderTab('finished', 'finished', false), true);
assert.equal(warehouseWorkspaceShouldRenderTab('finished', 'finished', true), false);
assert.equal(warehouseWorkspaceShouldRenderTab('semi_finished', 'finished', true), true);
const lookup = {{disabled: false, matches: () => false, closest: selector => selector === '#moldBindingPanel' ? {{closest: () => null}} : null}};
assert.equal(warehouseWorkspaceDirtySection(lookup), '');
const moldLabel = {{disabled: false, matches: selector => selector.includes('#moldPrimaryCustomer1'), closest: () => null}};
assert.equal(warehouseWorkspaceDirtySection(moldLabel), 'mold');
const finishedEntry = {{disabled: false, matches: () => false, closest: selector => selector === '#finishedForm' ? {{closest: () => null}} : null}};
assert.equal(warehouseWorkspaceDirtySection(finishedEntry), 'finishedEntry');
console.log('workspace helpers passed');
"""
    completed = subprocess.run(
        ["node", "-e", program],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout



def _run_initial_ledger_render_behavior() -> str:
    source = "\n".join(
        _function_source(name)
        for name in (
            "warehouseWorkspaceActivationPlan",
            "warehouseWorkspaceShouldRenderTab",
            "applyWarehouseDeepLink",
        )
    )
    program = f"""
const assert = require('node:assert/strict');
global.window = {{location: {{origin: 'https://erp.test'}}}};
global.location = {{search: ''}};
global.state = {{tab: 'finished'}};
global.warehouseWorkspace = {{initialTabRendered: false, submittedQuery: ''}};
const keyword = {{value: ''}};
const elements = {{keywordFilter: keyword, floorFilter: {{value: ''}}, areaFilter: {{}}, locationFilter: {{}}}};
global.$ = id => {{ if (elements[id]) return elements[id]; throw new Error(`unexpected element ${{id}}`); }};
let switchCalls = 0;
let inventoryVisible = false;
let locateCalls = 0;
const locateArgs = [];
global.refreshInventoryAreaOptions = () => {{}};
global.refreshInventoryLocationOptions = () => {{}};
global.switchTab = async tab => {{ switchCalls += 1; state.tab = tab; warehouseWorkspace.initialTabRendered = true; inventoryVisible = tab === 'finished'; return true; }};
global.locateWarehouseLedger = async args => {{ locateCalls += 1; locateArgs.push(args); return true; }};
global.publishWarehouseWorkspaceContext = () => {{}};
{source}
(async () => {{
  await applyWarehouseDeepLink(new URLSearchParams('tab=finished&q=P007'));
  assert.equal(switchCalls, 1);
  assert.equal(inventoryVisible, true);
  assert.equal(keyword.value, 'P007');
  assert.equal(locateCalls, 1);
  assert.equal(locateArgs[0].keyword, 'P007');
  assert.equal(locateArgs[0].scroll, false);
  await applyWarehouseDeepLink(new URLSearchParams('tab=finished&q=P007'));
  assert.equal(switchCalls, 1);
  warehouseWorkspace.initialTabRendered = false;
  warehouseWorkspace.submittedQuery = '';
  locateCalls = 0;
  await applyWarehouseDeepLink(new URLSearchParams());
  assert.equal(switchCalls, 2);
  assert.equal(inventoryVisible, true);
  assert.equal(locateCalls, 0);
  state.tab = 'semi_finished';
  warehouseWorkspace.initialTabRendered = true;
  await applyWarehouseDeepLink(new URLSearchParams('tab=semi_finished&search_floor=3F'));
  assert.equal(switchCalls, 2);
  assert.equal(state.tab, 'semi_finished');
  assert.equal(locateArgs.at(-1).inventoryType, 'semi_finished');
  assert.equal(locateArgs.at(-1).scroll, false);
  console.log('initial ledger render passed');
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", program],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout

def test_ledger_uses_the_same_origin_keepalive_workspace_protocol() -> None:
    assert 'message.source!=="tianming-erp-shell"' in LEDGER
    assert 'source:"tianming-warehouse"' in LEDGER
    assert 'event.origin!==window.location.origin||event.source!==window.parent' in LEDGER
    assert '"/warehouse.html","/warehouse-ledger.html"' in LEDGER
    assert 'warehouse-workspace-navigate' in LEDGER
    assert 'warehouse-workspace-blocked' in LEDGER
    assert 'command==="refresh"' in LEDGER


def test_ledger_keeps_real_lot_and_location_identity_when_opening_map() -> None:
    assert 'location_id:String(row.id),lot_id:lotId||""' in LEDGER
    assert 'openMeasuredWarehouseLocation(${Number(row.location.id)},${Number(row.id)})' in LEDGER
    assert 'state.selectedLotId=Number(lotId)' in LEDGER
    assert 'data-ledger-lot-id=' in LEDGER
    assert 'state.lotPage=1;const loaded=await loadLots()' in LEDGER


def test_ledger_only_shares_committed_query_and_explicit_search_floor() -> None:
    assert 'warehouseWorkspace.submittedQuery=$("keywordFilter").value.trim()' in LEDGER
    assert 'search_floor:warehouseLedgerSearchFloor()' in LEDGER
    assert 'const committedQ=warehouseWorkspace.submittedQuery' in LEDGER
    assert '$("floorFilter").value=searchFloor==="ALL"?"":searchFloor.slice(0,-1)' in LEDGER
    assert 'searchFloor==="UNLOCATED"' in LEDGER
    assert 'scope_changed:scopeChanged' in LEDGER
    assert 'target.searchParams.set("floor",warehouseLedgerSearchFloor())' not in LEDGER


def test_help_popover_and_pending_write_guard_are_keyboard_accessible() -> None:
    assert 'id="warehouseSearchHelp"' in LEDGER
    assert 'aria-expanded="false"' in LEDGER
    assert 'event.key==="Escape"&&!helpPopover.hidden' in LEDGER
    assert 'event.target.closest(".warehouse-help")' in LEDGER
    assert '当前保存、解绑或标签登记结果未确定' in LEDGER
    assert '当前关键编辑尚未保存' in LEDGER


def test_workspace_command_helpers_execute_real_ledger_functions() -> None:
    output = _run_workspace_functions(
        "warehouseWorkspaceActivationPlan",
        "warehouseWorkspaceActivationNeedsApply",
        "warehouseLedgerInventoryType",
        "warehouseWorkspaceShouldRenderTab",
        "warehouseWorkspaceDirtySection",
    )
    assert output.strip() == "workspace helpers passed"


def test_tab_guard_and_section_specific_drafts_are_preserved() -> None:
    assert 'if(tabChanged){const blocked=warehouseUncertainWriteMessage();if(blocked){toast(blocked,true);return false}}' in LEDGER
    assert 'if(tab==="stocktake_review"&&!canViewStocktakes())' in LEDGER
    assert '当前账号没有盘点记录查看权限，请联系管理员。' in LEDGER
    assert 'warehouseWorkspace.dirtySections.add("mold")' in LEDGER
    assert 'clearWarehouseWorkspaceDirty("mold")' in LEDGER
    assert 'clearWarehouseWorkspaceDirty("finishedEntry")' in LEDGER
    assert 'clearWarehouseWorkspaceDirty("semiEntry")' in LEDGER
    assert 'runWarehouseMutation' in LEDGER


def test_initial_ledger_route_renders_once_and_embedded_hides_duplicate_tabs() -> None:
    assert 'initialTabRendered:false' in LEDGER
    assert 'warehouseWorkspaceShouldRenderTab("finished",state.tab,warehouseWorkspace.initialTabRendered)&&await switchTab("finished")' in LEDGER
    assert 'else if(!requestedTab&&searchFloor&&searchFloor!=="UNLOCATED"' in LEDGER
    assert 'warehouseWorkspace.initialTabRendered=true;' in LEDGER
    assert '$("inventorySection").classList.toggle("hidden",!["finished","semi_finished"].includes(tab))' in LEDGER
    assert 'body.embedded>.wrap>.tabs>[data-tab]' in LEDGER
    assert 'body.embedded>.wrap>.warehouse-secondary [data-tab="molds"]' in LEDGER
    assert 'body.embedded>.wrap>.tabs .warehouse-help' in LEDGER
    assert 'lastActivation.searchFloor||warehouseLedgerSearchFloor()' in LEDGER
    assert 'warehouseWorkspace.lastActivation.searchFloor=null;warehouseWorkspace.scopeChanged=true' in LEDGER


def test_initial_ledger_route_runs_real_same_tab_render_once() -> None:
    assert _run_initial_ledger_render_behavior().strip() == "initial ledger render passed"

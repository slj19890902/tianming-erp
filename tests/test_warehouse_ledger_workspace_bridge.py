from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LEDGER = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\)\{{", LEDGER)
    assert match, f"missing {name}"
    depth = 0
    for index in range(match.start(), len(LEDGER)):
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
const semi = warehouseWorkspaceActivationPlan('/warehouse-ledger.html?tab=semi_finished&q=半成品');
assert.equal(warehouseWorkspaceActivationNeedsApply(semi, current), true);
assert.equal(warehouseLedgerInventoryType(null, 'semi_finished', 'finished'), 'semi_finished');
assert.equal(warehouseLedgerInventoryType({{inventory_type: 'finished'}}, 'semi_finished', 'semi_finished'), 'finished');
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

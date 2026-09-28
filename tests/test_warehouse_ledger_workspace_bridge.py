from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


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

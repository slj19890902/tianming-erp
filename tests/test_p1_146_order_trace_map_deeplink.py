from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
TWIN_APP = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
TWIN_CSS = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseTwin.css"
).read_text(encoding="utf-8")


def test_order_trace_location_opens_exact_batch_in_new_read_only_map_page() -> None:
    assert 'v-if="lot.map_deep_link"' in INDEX
    assert ':href="lot.map_deep_link"' in INDEX
    assert 'target="_blank"' in INDEX
    assert 'rel="noopener noreferrer"' in INDEX
    assert "lot.location_issue" in INDEX
    assert "lot.physical_quantity" in INDEX
    trace_inventory = INDEX[
        INDEX.index("当前关联库存位置") : INDEX.index("真实单据时间线")
    ]
    assert "lot.location_code" not in trace_inventory
    assert "lot.pallet_code" not in trace_inventory


def test_trace_map_deep_link_is_refresh_stable_and_forces_lookup_mode() -> None:
    assert 'query.get("readonly") === "1"' in TWIN_APP
    assert 'query.get("source") === "order_trace"' in TWIN_APP
    assert 'query.get("location_id")' in TWIN_APP
    assert 'query.get("lot_id")' in TWIN_APP
    assert 'if (traceReadOnly) return "lookup"' in TWIN_APP
    assert 'traceReadOnly\n      ? "lookup"' in TWIN_APP
    assert "setTraceFocusedLotId(pendingLotId)" in TWIN_APP
    assert "traceFocusedLotId === item.lot_id" in TWIN_APP
    assert "订单追溯 · 只读定位" in TWIN_APP


def test_trace_map_reports_stale_or_inaccessible_location_without_write_controls() -> None:
    assert "指定库位不存在、已停用，或当前账号无权查看" in TWIN_APP
    assert "指定成品批次已移位、清零，或当前账号无权查看" in TWIN_APP
    assert "!traceReadOnly && (canExecuteWarehouse || canStocktake)" in TWIN_APP
    assert "!traceReadOnly && canEditLocations" in TWIN_APP
    assert 'setCanEditLocations(!traceReadOnly && value.user.role === "admin")' in TWIN_APP
    assert 'setCanExecuteWarehouse(!traceReadOnly && value.permissions.includes("warehouse.execute"))' in TWIN_APP
    assert 'setCanStocktake(!traceReadOnly && value.permissions.includes("warehouse.stocktake.submit"))' in TWIN_APP
    assert "if (traceReadOnly || !canEditLocations || spatialEditBusy) return" in TWIN_APP
    assert "if (traceReadOnly || (!canExecuteWarehouse && !canStocktake) || spatialEditBusy) return" in TWIN_APP
    assert "!traceReadOnly && !!dashboard?.delayed_dispatch_relocation?.candidate_count" in TWIN_APP
    assert "!traceReadOnly && delayedDispatchOpen" in TWIN_APP
    assert ".twin-deeplink-message" in TWIN_CSS
    assert ".twin-deeplink-message.error" in TWIN_CSS


def test_trace_read_only_query_cannot_reopen_move_or_planning_modes() -> None:
    assert '!traceReadOnly && query.get("edit") === "area_policy"' in TWIN_APP
    assert '!traceReadOnly && query.get("edit") === "rack"' in TWIN_APP
    assert "if (traceReadOnly) return;\n    if (mapMode !== \"move\")" in TWIN_APP
    assert "traceReadOnly\n      || (!pendingAreaPolicyEdit && !pendingRackEdit)" in TWIN_APP
    assert "if (traceReadOnly || !pendingRackEdit || !locationEditMode || !selectedRack) return" in TWIN_APP


def test_built_warehouse_map_contains_trace_read_only_contract() -> None:
    built_assets = list(
        (ROOT / "static" / "factory-twin-assets" / "assets").glob(
            "warehouseTwin-*.js"
        )
    )
    assert len(built_assets) == 1
    built = built_assets[0].read_text(encoding="utf-8")
    assert "order_trace" in built
    assert "订单追溯 · 只读定位" in built
    assert "指定成品批次已移位、清零" in built

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SOURCE = (ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx").read_text(encoding="utf-8")
CSS = (ROOT / "factory_twin" / "frontend" / "src" / "warehouseTwin.css").read_text(encoding="utf-8")
MERGE = (ROOT / "factory_twin" / "frontend" / "src" / "warehousePalletMergeDraft.mjs").read_text(encoding="utf-8")
CANVAS = (ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx").read_text(encoding="utf-8")


def test_warehouse_navigation_is_integrated_into_the_erp_topbar() -> None:
    assert 'class="warehouse-top-shortcuts"' in INDEX
    assert "selectWarehouseFloor('1F')" in INDEX
    assert "selectWarehouseFloor('3F')" in INDEX
    assert 'ref="warehouseFrame"' in INDEX
    assert 'source: "tianming-erp-shell"' in INDEX
    assert "新增或编辑生产模具，请进入模具档案" not in INDEX
    assert "warehouse-shell-head" not in INDEX


def test_embedded_map_uses_one_compact_chinese_command_row() -> None:
    assert ".warehouse-twin-shell.embedded-shell" in CSS
    assert ".warehouse-twin-shell.embedded-shell .twin-command-bar" in CSS
    assert 'className="twin-toolbar"' in SOURCE
    toolbar = SOURCE[SOURCE.index('<section className="twin-toolbar">'):SOURCE.index('<section className={`twin-workspace')]
    assert "twin-warehouse-search-toggle" not in toolbar
    assert '<header><h2>查货</h2></header>' in SOURCE
    assert "setSearchPanelOpen(true)" in SOURCE[SOURCE.index("const returnToLookupMode"):SOURCE.index("const enterWarehouseMoveMode")]
    for obsolete in (
        "TIANMING WAREHOUSE",
        "WAREHOUSE SEARCH",
        "ERP INVENTORY",
        "VIEW LAYERS",
        "LIVE MOLD ASSET ELEVATION",
        "2.5D 等距",
    ):
        assert obsolete not in SOURCE


def test_employee_inventory_cards_hide_internal_pallet_codes_and_use_short_names() -> None:
    assert "customer_short_name" in MERGE
    assert "function employeeCustomerName" in SOURCE
    assert "已绑定实物栈板" in SOURCE
    assert "当前无栈板" in SOURCE
    assert "<b>{item.pallet_code}</b>" not in SOURCE
    assert "真实栈板 · {pallet.pallet_code}" not in SOURCE
    assert "{item.location_name} / {item.pallet_code}" not in SOURCE


def test_delayed_dispatch_rows_focus_the_real_map_location_before_optional_move() -> None:
    assert "focusDelayedDispatchCandidate" in SOURCE
    assert "延期待送地图定位" in SOURCE
    assert "整理移货" in SOURCE
    assert 'disabled={!canExecuteWarehouse || !candidate.can_plan_move} onClick={() => prepareDelayedDispatchMove(candidate)}' not in SOURCE


def test_merge_panel_is_compact_readable_and_highlights_selected_locations() -> None:
    for obsolete in (
        "合并集合已选 {mergeSources.length} 块；可切楼层和位置继续选择",
        "同存货编码、同规格可合并建议（只勾选现场要合并的栈板）",
        "还需选择 {2 - mergeSources.length} 块兼容系统栈板",
        "从集合内明确一个主货位；失败后选择、目标和重试键都会保留。",
    ):
        assert obsolete not in SOURCE
    assert "mergeHighlightPalletIds" in SOURCE
    assert "mapHighlightPalletIds" in SOURCE
    assert "twin-merge-selection-line" in SOURCE
    assert ".twin-workspace.merge-active" in CSS


def test_map_palette_and_employee_text_are_visible_without_ring_markers() -> None:
    highlight_source = CANVAS.split("function addEntityHighlight", 1)[1].split("function syncEntityHighlights", 1)[0]
    assert "RingGeometry" not in highlight_source
    assert "BoxGeometry" in highlight_source
    assert "0x7c3aed" in CANVAS
    assert "#dcefe3" in (ROOT / "factory_twin" / "frontend" / "src" / "operationalView.mjs").read_text(encoding="utf-8")
    assert "--warehouse-min-readable-size: 12px" in CSS


def test_warehouse_metrics_move_to_the_erp_topbar_and_leave_the_map_toolbar_compact() -> None:
    assert 'class="warehouse-top-metrics"' in INDEX
    assert "warehouseTwinMetrics.active_lots" in INDEX
    assert "warehouseTwinMetrics.occupied_locations" in INDEX
    assert "warehouseTwinMetrics.mapped_locations" in INDEX
    assert "warehouseTwinMetrics.unlocated_finished" in INDEX
    assert "warehouseTwinMetrics.column_conflicts" in INDEX
    assert "active_lots: currentFloor?.active_lots || 0" in SOURCE
    assert "occupied_locations: currentFloorOccupiedLocations" in SOURCE
    assert 'className="twin-toolbar-summary"' not in SOURCE


def test_move_actions_use_the_toolbar_space_and_inventory_heading_is_removed() -> None:
    toolbar = SOURCE[SOURCE.index('<section className="twin-toolbar">'):SOURCE.index('<section className={`twin-workspace')]
    operation_modes = toolbar[toolbar.index('<div className="twin-operation-modes"'):toolbar.index('</div>', toolbar.index('<div className="twin-operation-modes"'))]
    inspector = SOURCE[SOURCE.index('<aside className="twin-inspector">'):SOURCE.index('</aside>', SOURCE.index('<aside className="twin-inspector">'))]
    assert 'className="twin-toolbar-move-actions"' in toolbar
    assert '>自动合并</button>' not in operation_modes
    assert 'onClick={openAutomaticMerge}>合并栈板</button>' in toolbar
    for label in ("移动位置", "地图存放", "合并栈板", "盘点调整"):
        assert f">{label}</button>" in toolbar
    assert 'className="twin-toolbar-move-actions"' in toolbar[:toolbar.index('className="twin-toolbar-view-tools"')]
    assert '!!dashboard?.delayed_dispatch_relocation?.candidate_count' in toolbar
    assert "库存与库位" not in inspector
    assert '<header><h2>' not in inspector


def test_area_planning_uses_compact_adaptive_fields_and_short_actions() -> None:
    planner = SOURCE[SOURCE.index('className="twin-zone-simple-planner"'):SOURCE.index('className="twin-mold-rack-planner"')]
    assert '<b>用途与容量</b>' in planner
    assert ': "确认"}</button>' in planner
    assert '>编辑</button>' in planner
    assert '>货位/货架</button>' in planner
    assert '>发布</button>' in planner
    for obsolete in ("确认并启用此区域", "编辑区域", "整理货位/货架", "预览并发布"):
        assert obsolete not in planner
    assert "grid-template-columns: repeat(auto-fit, minmax(100px, 1fr))" in CSS
    assert ".twin-zone-simple-planner > header" in CSS
    assert ".twin-zone-simple-planner > .twin-location-point-planner" in CSS
    assert ".twin-zone-simple-planner > .twin-region-planning-actions" in CSS
    assert "overflow-y: hidden" in CSS


def test_merge_panel_exposes_customer_and_inventory_filters() -> None:
    assert 'aria-label="合并栈板客户筛选"' in SOURCE
    assert 'placeholder="输入存货编码、名称、规格或货位"' in SOURCE
    assert "filteredMergeSuggestions" in SOURCE
    assert "palletMergeSuggestionMatchesFilter" in SOURCE

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
    assert "全仓查找" in SOURCE
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

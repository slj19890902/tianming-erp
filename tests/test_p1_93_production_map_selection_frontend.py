from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_history_uses_compact_columns_and_map_selection_handoff() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'class="production-compact-command-bar"' in source
    for heading in (
        "序号",
        "客户单号",
        "客户",
        "存货编码 / 产品尺寸",
        "数量",
        "完工时间",
        "去向 / 库位",
        "操作",
    ):
        assert heading in source
    assert "openProductionLocationSelection(row)" in source
    assert "location-selection-sessions" in source
    assert "stock-transfers/from-selection" in source
    assert "handleProductionLocationSelectionMessage" in source
    assert "openProductionInventoryMap(row)" in source


def test_warehouse_twin_has_selection_only_mode_and_zero_write_confirmation() -> None:
    source = (
        ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
    ).read_text(encoding="utf-8")

    assert 'query.get("selection_token")' in source
    assert "production-location-selection" in source
    assert "candidate_locations" in source
    assert "writes_inventory" in source
    assert "tianming:production-location-selected" in source
    assert "地图选位不会移动库存" in source

    canvas = (
        ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx"
    ).read_text(encoding="utf-8")
    assert "selectionPriorityPalletIds" in source
    assert "selectionPriorityPalletIdSet" in canvas
    assert "priorityRoot || entityNode" in canvas


def test_built_warehouse_twin_contains_production_selection_contract() -> None:
    assets = ROOT / "static" / "factory-twin-assets" / "assets"
    scripts = list(assets.glob("warehouseTwin-*.js"))
    assert scripts
    bundle = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
    assert "production-location-selection" in bundle
    assert "tianming:production-location-selected" in bundle

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
SHELL = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
COST_PAGE = (
    ROOT / "static" / "factory-twin-assets" / "warehouse-costs.html"
).read_text(encoding="utf-8")
TWIN = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")


def test_map_keeps_ledger_but_removes_duplicate_outer_cost_entry() -> None:
    command_start = TWIN.index('<nav className="twin-command-links"')
    command_end = TWIN.index("</nav>", command_start)
    command_nav = TWIN[command_start:command_end]
    toolbar_start = TWIN.index('<nav className="twin-top-ledger"')
    toolbar_end = TWIN.index("</nav>", toolbar_start)
    toolbar_nav = TWIN[toolbar_start:toolbar_end]

    for nav in (command_nav, toolbar_nav):
        assert ">库存台账</a>" in nav
        assert "库存成本" not in nav
        assert "warehouse-costs.html" not in nav


def test_ledger_keeps_cost_entry_and_stocktake_returns_through_erp_shell() -> None:
    assert (
        'id="warehouseCostLink" class="btn hidden" '
        'href="/factory-twin-assets/warehouse-costs.html"'
    ) in LEDGER
    assert (
        'id="mapStocktakeLink" class="btn hidden" '
        'href="/?page=warehouse&amp;warehouse_mode=move&amp;'
        'warehouse_action=stocktake&amp;warehouse_view=2d" target="_top"'
    ) in LEDGER


def test_shell_forwards_stocktake_intent_to_embedded_warehouse() -> None:
    assert 'const requestedMode = query.get("warehouse_mode") || "";' in SHELL
    assert 'params.set("mode", requestedMode);' in SHELL
    assert 'params.set("action", requestedAction);' in SHELL
    assert 'this.warehouseFrameUrl = `/warehouse.html?${params.toString()}`;' in SHELL


def test_cost_page_returns_to_erp_shell() -> None:
    assert '<a href="/?page=warehouse" target="_top">返回仓库</a>' in COST_PAGE
    assert '<a href="/warehouse.html">返回仓库</a>' not in COST_PAGE

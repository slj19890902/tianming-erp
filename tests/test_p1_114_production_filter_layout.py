from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _production_markup() -> str:
    return INDEX.split('<template v-else-if="activePage === \'production\'">', 1)[
        1
    ].split('<template v-else-if="activePage === \'deliveries\'">', 1)[0]


def _method(start: str, end: str) -> str:
    return INDEX.split(start, 1)[1].split(end, 1)[0]


def test_production_tabs_stay_in_a_dedicated_non_wrapping_strip() -> None:
    production = _production_markup()
    command_bar = production.split('class="production-compact-command-bar"', 1)[
        1
    ].split('</div>\n              <div', 1)[0]

    assert 'class="toolbar-group production-tab-strip"' in command_bar
    assert "production-history-filters" not in command_bar
    assert "把一楼待送区成品归位" not in command_bar
    assert ".production-tab-strip { flex:0 0 auto; flex-wrap:nowrap;" in INDEX
    assert ".production-compact-command-bar {" in INDEX
    assert "flex-wrap:nowrap" in INDEX.split(
        ".production-compact-command-bar {", 1
    )[1].split("}", 1)[0]


def test_history_and_placement_filters_only_expand_after_explicit_click() -> None:
    production = _production_markup()

    assert "productionHistoryFilterVisible" in INDEX
    assert "productionPlacementFilterVisible" in INDEX
    assert (
        "productionHistoryFilterVisible=!productionHistoryFilterVisible" in production
    )
    assert (
        "productionPlacementFilterVisible=!productionPlacementFilterVisible"
        in production
    )
    assert (
        "v-if=\"productionTab==='history' && productionHistoryFilterVisible\""
        in production
    )
    assert (
        "v-if=\"productionTab==='placement' && productionPlacementFilterVisible\""
        in production
    )
    assert production.count("筛选查找") >= 2
    assert 'class="production-filter-panel"' in production
    assert ".production-filter-panel {" in INDEX
    assert "flex-wrap:nowrap" in INDEX.split(
        ".production-filter-panel {", 1
    )[1].split("}", 1)[0]


def test_production_customer_matches_use_a_vertical_scrollable_dropdown() -> None:
    assert ".production-filter-panel .search-select-list" in INDEX
    dropdown = INDEX.split(
        ".production-filter-panel .search-select-list", 1
    )[1].split("}", 1)[0]
    option = INDEX.split(
        ".production-filter-panel .search-select-option", 1
    )[1].split("}", 1)[0]

    assert "overflow-y:auto" in dropdown
    assert "overflow-x:hidden" in dropdown
    assert "white-space:normal" in option
    assert "display:block" in option


def test_placement_query_uses_its_own_filters_and_reset_contract() -> None:
    placement = _method(
        "async loadProductionPlacement() {",
        "resetProductionHistoryFilters() {",
    )
    reset = _method(
        "resetProductionPlacementFilters() {",
        "productionLocation(locationId) {",
    )

    assert "this.productionPlacementFilters" in placement
    assert "params[key] = value" in placement
    assert "placement_pending:true" in placement
    assert "this.productionPlacementFilters =" in reset
    assert "this.pages.productionPlacement = 1" in reset
    assert "this.loadProductionPlacement()" in reset


def test_cold_production_entry_loads_customer_options_with_page_data() -> None:
    production_loader = _method(
        "async loadProduction() {",
        "async openProductionLabelMaintenance() {",
    )

    assert "this.loadCustomerOptions()" in production_loader
    assert "this.loadProductionHistory()" in production_loader
    assert "this.loadProductionPlacement()" in production_loader
    assert "Promise.all" in production_loader

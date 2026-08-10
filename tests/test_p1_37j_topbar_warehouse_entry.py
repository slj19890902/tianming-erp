from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start : INDEX.index(end_marker, start)]


def test_warehouse_entry_is_permission_scoped_and_precedes_topbar_identity_and_mode() -> None:
    topbar = _block('<div class="top-actions">', "</header>")
    warehouse = topbar.index("topbar-warehouse-entry")
    user_name = topbar.index("top-user-name")
    role = topbar.index("roleLabel(user.role)")
    display_mode = topbar.index("ui-mode-switch")

    assert warehouse < user_name < role < display_mode
    assert 'v-if="pageAllowed(\'warehouse\')"' in topbar
    assert '@click="goMenu(\'warehouse\')"' in topbar
    assert ">仓库</span>" in topbar
    assert 'warehouse:"warehouse.view"' in INDEX


def test_warehouse_entry_is_not_duplicated_in_sidebar_or_workbench_navigation() -> None:
    menu = _block("menus() {", "deliveryCustomers() {")
    navigation = _block("navigationGroups() {", "currentNavigationGroup() {")

    assert '{ key: "warehouse", label: "仓库地图" }' not in menu
    assert '{key:"warehouse",label:"仓库地图"}' not in navigation
    assert INDEX.count("topbar-warehouse-entry") == 3


def test_topbar_entry_reuses_existing_navigation_without_new_api_or_window() -> None:
    topbar = _block('<div class="top-actions">', "</header>")
    go_menu = _block("async goMenu(menuKey) {", "async openWarehouseMap() {")

    assert "axios" not in topbar
    assert "window.open" not in topbar
    assert "if (!group) return this.go(menuKey)" in go_menu
    assert "axios" not in go_menu
    assert "/api/" not in go_menu

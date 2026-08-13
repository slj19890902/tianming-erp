from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"
INDEX = INDEX_PATH.read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start : INDEX.index(end_marker, start)]


def test_order_flow_is_first_and_duplicate_workbench_tabs_are_not_rendered() -> None:
    shell = _block(
        '<main :class="[\'main\'',
        '<section v-if="orderNextStepGuide.visible',
    )
    assert shell.index('<section v-if="businessFlowCurrentStep"') < shell.index(
        '<section v-if="currentNavigationGroup'
    )
    assert "currentNavigationGroup && currentNavigationGroup.key!=='workbench'" in shell

    navigation = _block("navigationGroups() {", "currentNavigationGroup() {")
    workbench = navigation[navigation.index('{ key:"workbench"') : navigation.index("]},")]
    assert 'label:"订单主链"' in workbench
    for label in ("订单", "报料", "来料入库"):
        assert f'label:"{label}"' in workbench
    assert "仓库地图" not in workbench


def test_warehouse_map_is_a_permission_scoped_topbar_item_and_not_in_sidebar() -> None:
    menu = _block("menus() {", "deliveryCustomers() {")
    assert '{ key: "dashboard", label: "首页" }' in menu
    assert '{ key: "warehouse", label: "仓库地图" }' not in menu
    assert 'key: "workbench", label: "订单主链"' in menu
    assert "this.pageAllowed(item.key)" in menu
    assert 'warehouse:"warehouse.view"' in INDEX
    assert 'boss: ["dashboard", "customers", "products", "orders", "requisition", "incoming", "production", "warehouse"' in INDEX

    topbar = _block('<div class="top-actions">', "</header>")
    assert 'v-if="pageAllowed(\'warehouse\')"' in topbar
    assert '@click="goMenu(\'warehouse\')"' in topbar
    assert topbar.index("topbar-warehouse-entry") < topbar.index("ui-mode-switch")

    go_menu = _block("async goMenu(menuKey) {", "async openWarehouseMap() {")
    assert "if (!group) return this.go(menuKey)" in go_menu
    assert "axios" not in go_menu
    assert "/api/" not in go_menu


def test_navigation_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        source
        for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if source.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-37a-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

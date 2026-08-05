from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _page(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start : INDEX.index(end_marker, start)]


def _page_from(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


def test_primary_workbench_tabs_are_fixed_order_permission_scoped_and_left_aligned() -> None:
    navigation = _page("navigationGroups() {", "currentNavigationGroup() {")
    markers = [
        '{key:"orders",label:"订单"}',
        '{key:"requisition",label:"报料"}',
        '{key:"incoming",label:"来料入库"}',
        '{key:"warehouse",label:"仓库地图"}',
    ]
    positions = [navigation.index(marker) for marker in markers]
    assert positions == sorted(positions)

    shell = _page(
        '<section v-if="currentNavigationGroup"',
        '<section v-if="warehouseFrameUrl"',
    )
    assert "'workbench-nav-primary':currentNavigationGroup.key==='workbench'" in shell
    assert 'v-if="pageAllowed(page.key)"' in shell
    assert "active:activePage===page.key" in shell
    assert ".workbench-nav-primary .workbench-tabs { order:-1" in INDEX
    assert ".workbench-nav-primary .workbench-nav-title { flex:1 1 auto; text-align:right; }" in INDEX


def test_order_second_row_keeps_creation_left_and_refresh_right() -> None:
    page = _page(
        "<template v-else-if=\"activePage === 'orders'\">",
        "<template v-else-if=\"activePage === 'requisition'\">",
    )
    actionbar = _page_from(
        page,
        '<div class="order-page-actionbar">',
        '<div class="order-filter-card">',
    )
    assert actionbar.index(">新建订单<") < actionbar.index(">识别PDF订单<") < actionbar.index(">刷新<")
    assert actionbar.count('v-if="canCreateOrders"') == 2
    assert "<h2" not in actionbar


def test_requisition_actions_are_separated_and_fixed_in_requested_order() -> None:
    page = _page(
        "<template v-else-if=\"activePage === 'requisition'\">",
        "<template v-else-if=\"activePage === 'incoming'\">",
    )
    assert ">新建订单<" not in page
    assert ">识别PDF订单<" not in page
    assert '>待报料 {{ requisitionPending.length }}<' in page
    assert ">已报料/已入库<" in page
    assert '@click="openSupplierRequisitionDraft()">合并报料</button>' in page

    actions = _page_from(
        page,
        '<div class="requisition-page-actions">',
        "</div>\n              </div>",
    )
    markers = [
        ">暂不报料<",
        '@click="openStockReplenishment">库存补库',
        ">等候报料 ",
        ">刷新<",
    ]
    positions = [actions.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "requisitionTab!=='pending' || !selectedPendingKeys.length" in actions
    assert actions.count('v-if="canRequisition"') == 2
    assert "手动库存补库" not in actions


def test_incoming_and_warehouse_do_not_gain_order_entry_actions() -> None:
    incoming = _page(
        "<template v-else-if=\"activePage === 'incoming'\">",
        "<template v-else-if=\"activePage === 'production'\">",
    )
    warehouse = _page(
        '<section v-if="warehouseFrameUrl"',
        "<template v-if=\"activePage === 'dashboard'\">",
    )
    for page in (incoming, warehouse):
        assert ">新建订单<" not in page
        assert ">识别PDF订单<" not in page


def test_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-26a-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

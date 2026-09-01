from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"
INDEX = INDEX_PATH.read_text(encoding="utf-8")


def _headless_browser() -> Path | None:
    for command in ("msedge", "chrome", "chromium"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved)
    candidates: list[Path] = []
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidates.extend(
            (
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
            )
        )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _style_source() -> str:
    return INDEX.split("<style>", 1)[1].split("</style>", 1)[0]


def _render_probe(tmp_path: Path, mode: str, *, expanded: bool = False) -> str:
    browser = _headless_browser()
    if browser is None:
        pytest.skip("当前环境未找到 Edge/Chrome，跳过实际 1920×1080 DOM 验收")
    row_count = 6 if mode == "large" else 11
    rows = "".join(
        f"<tr><td>{index}</td><td>聚晟达<div class='muted'>客户简称</div></td><td>61452621R1F</td><td>当前业务状态完整可见<div class='muted'>ERP录入 2026-09-01 10:30</div></td><td><button class='btn small'>查看</button></td></tr>"
        for index in range(1, row_count + 1)
    )
    expansion = (
        "<section class='panel explicit-expansion' style='min-height:900px;padding:18px'>"
        "用户主动展开后的完整业务明细</section>"
        if expanded
        else ""
    )
    fixture = tmp_path / f"p1-138b-{mode}-{'expanded' if expanded else 'default'}.html"
    fixture.write_text(
        f"""<!doctype html><html><head><meta charset='utf-8'><style>{_style_source()}</style></head>
<body><div class='app-shell erp-enterprise-ui ui-{mode}'>
  <header class='topbar'><div class='brand'>天明包装ERP</div><div class='top-actions'><button class='btn'>刷新</button></div></header>
  <div class='layout'><aside class='sidebar'><button class='menu-item active'>送货与回单</button></aside>
  <main class='main single-screen-main' data-single-screen-page='deliveries' data-single-screen-mode='{mode}' data-single-screen-expanded='{'true' if expanded else 'false'}'>
    <section class='business-flow-guide'><span class='business-flow-guide-label'>按顺序做</span><div class='business-flow-steps'><button class='business-flow-step'>1.订单</button><button class='business-flow-step current'>5.送货</button><button class='business-flow-step'>6.对账</button></div></section>
    <div class='page-head'><div><h2>送货与回单</h2><p>默认单屏容量验收</p></div><button class='btn primary'>新增送货单</button></div>
    <div class='list-filterbar delivery-list-filterbar'><input class='input delivery-keyword-input' value='聚晟达'><button class='btn'>查询</button><button class='btn'>详细筛选</button></div>
    <div class='panel'><div class='table-wrap' id='table-wrap'><table><thead><tr><th>序号</th><th>客户</th><th>存货编码</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table></div><div class='pager' id='pager'>共 {row_count} 条，第 1 / 1 页</div></div>
    {expansion}
  </main></div>
</div><script>
(() => {{
  const main=document.querySelector('main');
  const wrap=document.getElementById('table-wrap');
  const pager=document.getElementById('pager');
  const lastRow=wrap.querySelector('tbody tr:last-child');
  const mainRect=main.getBoundingClientRect();
  const verticalScrollers=[main,...main.querySelectorAll('*')].filter(node=>{{
    const style=getComputedStyle(node);
    return /(auto|scroll)/.test(style.overflowY) && node.scrollHeight>node.clientHeight+1;
  }});
  document.body.dataset.probeComplete='true';
  document.body.dataset.mainOverflow=String(main.scrollHeight>main.clientHeight+1);
  document.body.dataset.mainScrollHeight=String(main.scrollHeight);
  document.body.dataset.mainClientHeight=String(main.clientHeight);
  document.body.dataset.lastRowHeight=String(Math.round(lastRow.getBoundingClientRect().height));
  document.body.dataset.tableOverflow=String(wrap.scrollHeight>wrap.clientHeight+1);
  document.body.dataset.tableOverflowY=getComputedStyle(wrap).overflowY;
  document.body.dataset.lastRowVisible=String(lastRow.getBoundingClientRect().bottom<=mainRect.bottom+1);
  document.body.dataset.pagerVisible=String(pager.getBoundingClientRect().bottom<=mainRect.bottom+1);
  document.body.dataset.verticalScrollers=String(verticalScrollers.length);
}})();
</script></body></html>""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1920,1080",
            "--virtual-time-budget=1500",
            f"--user-data-dir={tmp_path / ('profile-' + mode + ('-expanded' if expanded else ''))}",
            "--dump-dom",
            fixture.resolve().as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    return result.stdout


def test_reusable_contract_marks_ordinary_pages_and_keeps_warehouse_separate() -> None:
    assert "'single-screen-main': activePage !== 'warehouse'" in INDEX
    assert ':data-single-screen-page="activePage"' in INDEX
    assert ':data-single-screen-mode="uiMode"' in INDEX
    assert ':data-single-screen-expanded="singleScreenExpanded ? \'true\' : \'false\'"' in INDEX
    assert ".main.warehouse-main { display:grid; grid-template-rows:minmax(0,1fr); gap:0; overflow:hidden; }" in INDEX


def test_top_level_paged_tables_delegate_vertical_overflow_to_main() -> None:
    contract = INDEX.split("/* P1-138B:", 1)[1].split("@media (max-width: 1500px)", 1)[0]
    assert ".main.single-screen-main > .table-wrap" in contract
    assert ".main.single-screen-main > .panel > .table-wrap" in contract
    assert "max-height: none" in contract
    assert "overflow-y: visible" in contract
    assert ".modal" not in contract


def test_standard_and_large_modes_have_separate_default_capacities_and_reload() -> None:
    for marker in (
        "DESKTOP_LIST_PAGE_SIZE_STANDARD = 11",
        "DESKTOP_LIST_PAGE_SIZE_LARGE = 6",
        "pageSize: DESKTOP_LIST_PAGE_SIZE_STANDARD",
        "page_size:DESKTOP_LIST_PAGE_SIZE_STANDARD",
        "this.syncDesktopListPageSize(this.uiMode);",
        'if (this.activePage === "deliveries")',
        'if (this.activePage === "finance")',
        'if (this.activePage === "audit")',
        'if (this.productionTab === "pending")',
        "return this.loadProductionPage(1);",
    ):
        assert marker in INDEX


def test_master_data_client_lists_use_the_same_mode_specific_page_capacity() -> None:
    for marker in (
        "pagedProductCustomers()",
        "pagedSuppliers()",
        "pagedMaterials()",
        'v-for="row in pagedProductCustomers"',
        'v-for="row in pagedSuppliers"',
        'v-for="row in pagedMaterials"',
        ':page="pages.productCustomers"',
        ':page="pages.suppliers"',
        ':page="pages.materials"',
    ):
        assert marker in INDEX


def test_default_detail_areas_are_collapsed_and_each_list_has_one_pager() -> None:
    assert "dashboardDetailsVisible:false" in INDEX
    assert 'v-if="dashboardDetailsVisible" class="dashboard-detail-stack"' in INDEX
    assert "deliveryListFilterVisible:false" in INDEX
    assert 'v-if="deliveryListFilterVisible" class="delivery-advanced-filters"' in INDEX
    assert "auditFiltersVisible:false" in INDEX
    assert 'v-if="auditFiltersVisible" class="panel audit-filter-panel"' in INDEX
    assert "financeOverviewDetailsVisible:false" in INDEX
    assert 'v-if="financeOverviewDetailsVisible"' in INDEX

    deliveries = INDEX.split("<template v-else-if=\"activePage === 'deliveries'\">", 1)[1].split(
        "<template v-else-if=\"activePage === 'finance'\">", 1
    )[0]
    assert deliveries.count(':page="pages.deliveries"') == 1
    assert INDEX.count(':page="pages.customerHeat"') == 1


@pytest.mark.parametrize("mode", ["standard", "large"])
def test_default_1920x1080_list_has_no_vertical_scroll_and_keeps_last_row_and_pager_visible(
    tmp_path: Path, mode: str
) -> None:
    output = _render_probe(tmp_path, mode)
    for marker in (
        'data-probe-complete="true"',
        'data-main-overflow="false"',
        'data-table-overflow="false"',
        # CSS computes visible to auto when horizontal overflow is auto; the
        # no-overflow measurement below is the actual vertical-scroll contract.
        'data-table-overflow-y="auto"',
        'data-last-row-visible="true"',
        'data-pager-visible="true"',
        'data-vertical-scrollers="0"',
    ):
        assert marker in output


def test_explicit_expansion_uses_only_the_main_vertical_scroll_path(tmp_path: Path) -> None:
    output = _render_probe(tmp_path, "standard", expanded=True)
    assert 'data-probe-complete="true"' in output
    assert 'data-main-overflow="true"' in output
    assert 'data-table-overflow="false"' in output
    assert 'data-vertical-scrollers="1"' in output

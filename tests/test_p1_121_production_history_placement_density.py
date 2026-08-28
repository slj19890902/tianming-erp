import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _production_page() -> str:
    return INDEX.split('<template v-else-if="activePage === \'production\'">', 1)[1].split(
        '<template v-else-if="activePage === \'deliveries\'">', 1
    )[0]


def _placement_table() -> str:
    page = _production_page()
    return page.split('<table class="production-placement-table"', 1)[1].split(
        "</table>", 1
    )[0]


def test_history_adds_five_rows_and_uses_page_scroll_only() -> None:
    assert "PRODUCTION_HISTORY_PAGE_SIZE_STANDARD = 13" in INDEX
    assert "PRODUCTION_HISTORY_PAGE_SIZE_LARGE = 11" in INDEX
    assert ".table-wrap:has(> .production-placement-table)" in INDEX


def test_fully_delivered_history_rows_offer_no_modify_or_revert_action() -> None:
    page = _production_page()
    assert (
        "['admin','boss'].includes(user?.role) && !row.is_fully_delivered && "
        "row.can_adjust_actual_quantity"
    ) in page
    assert "user?.role==='admin' && !row.is_fully_delivered && row.can_revert" in page


def test_placement_uses_customer_po_short_name_and_compact_columns() -> None:
    table = _placement_table()
    assert '<th style="width:12%">客户订单号 / 中文简称</th>' in table
    assert '<th style="width:18%">存货编码 / 产品</th>' in table
    assert '<th style="width:8%">实际完工</th>' in table
    assert '<th style="width:18%">当前待送位置</th>' in table
    assert "row.customer_order_number || '客户订单号未填写'" in table
    assert "row.customer_short_name || '中文简称未填写'" in table
    assert "row.item_order_number || row.order_number" not in table
    assert "row.customer_name || '-'" not in table


def test_placement_location_controls_stay_in_one_compact_row() -> None:
    table = _placement_table()
    assert table.count('<select class="select"') == 3
    assert ">归位</button>" in table
    assert "归位并同步地图" not in table
    assert "grid-template-columns:74px 108px clamp(150px,14vw,210px) auto" in INDEX
    assert ".production-placement-table .production-location-picker" in INDEX
    assert ".production-placement-table .select" in INDEX
    assert ".production-placement-table .btn" in INDEX
    assert "-webkit-line-clamp:2" in INDEX
    assert "white-space:normal" in INDEX
    assert "height:32px" in INDEX


def _headless_browser() -> Path | None:
    for command in ("msedge", "chrome", "chromium"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved)
    roots = [os.environ.get(name) for name in ("PROGRAMFILES(X86)", "PROGRAMFILES")]
    candidates = [
        Path(root) / relative
        for root in roots
        if root
        for relative in (
            "Microsoft/Edge/Application/msedge.exe",
            "Google/Chrome/Application/chrome.exe",
        )
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def test_real_desktop_dom_keeps_placement_controls_on_one_row_without_inner_scroll(
    tmp_path: Path,
) -> None:
    browser = _headless_browser()
    if browser is None:
        pytest.skip("当前环境未找到 Edge/Chrome，跳过实际桌面 DOM 密度验收")
    styles = "\n".join(re.findall(r"<style\b[^>]*>.*?</style>", INDEX, re.S))
    history_rows = "".join(
        "<tr><td>2026-08-28 10:30</td><td>YL<br>PO-20260828</td>"
        "<td>Z.001.000205<br>组合产品名称</td><td>实际 1200<br>理论 1200</td>"
        "<td>3F-左区-成品-001</td><td>-</td></tr>"
        for _ in range(13)
    )
    fixture = tmp_path / "p1-121-production-density.html"
    fixture.write_text(
        f"""<!doctype html><meta charset=\"utf-8\">{styles}
        <body><div class=\"ui-large\" style=\"width:1580px\">
        <div class=\"table-wrap\" id=\"history-wrap\"><table class=\"production-history-table\"><tbody>{history_rows}</tbody></table></div>
        <div class=\"table-wrap\" id=\"placement-wrap\"><table class=\"production-placement-table\" style=\"table-layout:fixed;width:100%\">
        <thead><tr><th style=\"width:12%\">客户订单号 / 中文简称</th><th style=\"width:18%\">存货编码 / 产品</th><th style=\"width:8%\">实际完工</th><th style=\"width:18%\">当前待送位置</th><th>现场实际位置</th></tr></thead>
        <tbody><tr id=\"placement-row\"><td>PO-20260828<br>驿力</td><td>Z.001.000205<br>组合产品完整名称不得遮挡</td><td>1200</td><td><div id=\"current-location\" class=\"placement-current-location\">三楼左区成品仓库超长实际货位名称第二行完整展示</div></td><td><div class=\"production-location-picker\"><select class=\"select\"><option>3F</option></select><select class=\"select\"><option>左区成品</option></select><select class=\"select\" id=\"actual-location-select\"><option>3F-左区-成品-001</option></select><button class=\"btn small success\">归位</button></div></td></tr></tbody></table></div></div>
        <script>requestAnimationFrame(() => {{
          const controls=[...document.querySelectorAll('.production-location-picker > *')];
          const tops=controls.map(node => node.getBoundingClientRect().top);
          document.body.dataset.controlsOneRow=String(Math.max(...tops)-Math.min(...tops)<2);
          document.body.dataset.placementRowHeight=String(Math.round(document.getElementById('placement-row').getBoundingClientRect().height));
          document.body.dataset.historyRows=String(document.querySelectorAll('.production-history-table tbody tr').length);
          const historyWrap=document.getElementById('history-wrap');
          const placementWrap=document.getElementById('placement-wrap');
          document.body.dataset.historyMaxHeight=getComputedStyle(historyWrap).maxHeight;
          document.body.dataset.placementMaxHeight=getComputedStyle(placementWrap).maxHeight;
          document.body.dataset.historyNoInnerScroll=String(historyWrap.scrollHeight<=historyWrap.clientHeight+1);
          document.body.dataset.placementNoInnerScroll=String(placementWrap.scrollHeight<=placementWrap.clientHeight+1);
          const currentLocation=document.getElementById('current-location');
          const currentLineHeight=parseFloat(getComputedStyle(currentLocation).lineHeight);
          document.body.dataset.currentLocationLines=String(Math.round(currentLocation.getBoundingClientRect().height/currentLineHeight));
          document.body.dataset.actualLocationWidth=String(Math.round(document.getElementById('actual-location-select').getBoundingClientRect().width));
          document.body.dataset.complete='true';
        }});</script></body>""",
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
            "--virtual-time-budget=2000",
            f"--user-data-dir={tmp_path / 'profile'}",
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
    assert 'data-complete="true"' in result.stdout
    assert 'data-controls-one-row="true"' in result.stdout
    assert 'data-history-rows="13"' in result.stdout
    assert 'data-history-max-height="none"' in result.stdout
    assert 'data-placement-max-height="none"' in result.stdout
    assert 'data-history-no-inner-scroll="true"' in result.stdout
    assert 'data-placement-no-inner-scroll="true"' in result.stdout
    assert 'data-current-location-lines="2"' in result.stdout
    width_match = re.search(r'data-actual-location-width="(\d+)"', result.stdout)
    assert width_match and 150 <= int(width_match.group(1)) <= 215
    match = re.search(r'data-placement-row-height="(\d+)"', result.stdout)
    assert match and int(match.group(1)) <= 52

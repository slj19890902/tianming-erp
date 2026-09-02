from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _headless_chrome() -> Path | None:
    for command in ("chrome", "msedge", "chromium"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved)
    candidates: list[Path] = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        candidates.extend(
            (
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            )
        )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _style_source() -> str:
    return INDEX.split("<style>", 1)[1].split("</style>", 1)[0]


def _render_layout_probe(tmp_path: Path, mode: str) -> str:
    chrome = _headless_chrome()
    if chrome is None:
        pytest.skip("当前环境未找到 Chrome/Edge，跳过实际布局探针")
    fixture = tmp_path / f"p0-40-delivery-filter-{mode}.html"
    fixture.write_text(
        f"""<!doctype html><html><head><meta charset='utf-8'><style>{_style_source()}</style></head>
<body><div class='app-shell erp-enterprise-ui ui-{mode}'>
  <header class='topbar'><div class='brand'>天明包装ERP</div></header>
  <div class='layout'><aside class='sidebar'></aside><main class='main'>
    <div id='bar' class='delivery-detailed-filterbar delivery-advanced-filters'>
      <div class='field delivery-detailed-customer-filter'><label>客户</label><div class='search-select'><input class='input' aria-label='客户'><div id='customer-menu' class='search-select-list' style='display:none'><button class='search-select-option'>聚晟达</button><button class='search-select-option'>天华</button></div></div></div>
      <input class='input delivery-date-input' type='date' aria-label='送货开始日期'>
      <input class='input delivery-date-input' type='date' aria-label='送货结束日期'>
      <select class='select delivery-status-select' aria-label='送货状态'><option>全部送货状态</option></select>
      <select class='select delivery-status-select' aria-label='回单状态'><option>全部回单状态</option></select>
      <div class='field'><label>客户订单号</label><input class='input' aria-label='客户订单号'></div>
      <div class='field'><label>存货编码</label><input class='input' aria-label='存货编码'></div>
      <div class='field'><label>产品名称</label><input class='input' aria-label='产品名称'></div>
      <div class='delivery-detailed-filter-actions'><button class='btn primary'>查询详细条件</button><button class='btn'>清空详细条件</button></div>
    </div>
    <section class='panel' style='min-height:400px'>送货列表</section>
  </main></div>
</div><script>
(() => {{
  const bar=document.getElementById('bar');
  const controls=[...bar.querySelectorAll('input,select,button')];
  const items=[...bar.children];
  const rects=items.map(node=>node.getBoundingClientRect());
  const overlaps=rects.some((left,index)=>rects.slice(index+1).some(right=>
    left.left < right.right-1 && left.right > right.left+1 &&
    left.top < right.bottom-1 && left.bottom > right.top+1));
  const rowCount=new Set(rects.map(rect=>Math.round(rect.bottom))).size;
  const customerInput=bar.querySelector('[aria-label="客户"]');
  const customerMenu=document.getElementById('customer-menu');
  customerInput.focus();
  customerMenu.style.display='block';
  const customerOption=customerMenu.querySelector('button');
  const optionRect=customerOption.getBoundingClientRect();
  const optionHit=document.elementFromPoint(optionRect.left+optionRect.width/2,optionRect.top+optionRect.height/2);
  const dropdownOnTop=optionHit===customerOption;
  customerMenu.style.display='none';
  customerInput.blur();
  customerInput.addEventListener('focus',()=>customerMenu.style.display='block');
  customerMenu.querySelectorAll('button').forEach(option=>option.addEventListener('mousedown',()=>{{
    customerInput.value=option.textContent.trim();
    customerMenu.style.display='none';
  }}));
  const visibleControls=controls.filter(node=>node.getClientRects().length);
  const clickable=visibleControls.every(node=>{{
    const rect=node.getBoundingClientRect();
    const hit=document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2);
    return rect.width>=32 && rect.height>=32 && (hit===node || node.contains(hit));
  }});
  const readable=[...bar.querySelectorAll('input,select')].every(node=>node.getBoundingClientRect().width>=120);
  document.body.dataset.probeComplete='true';
  document.body.dataset.overlaps=String(overlaps);
  document.body.dataset.rowCount=String(rowCount);
  document.body.dataset.clickable=String(clickable);
  document.body.dataset.readable=String(readable);
  document.body.dataset.dropdownOnTop=String(dropdownOnTop);
  document.body.dataset.barOverflow=String(bar.scrollWidth>bar.clientWidth+1);
  document.body.dataset.mainOverflow=String(document.querySelector('main').scrollWidth>document.querySelector('main').clientWidth+1);
}})();
</script></body></html>""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1920,1080",
            "--virtual-time-budget=1000",
            f"--user-data-dir={tmp_path / ('profile-' + mode)}",
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


def test_detailed_filter_uses_safe_grid_and_preserves_detailed_clear_contract() -> None:
    page = INDEX.split("activePage === 'deliveries'", 1)[1].split("activePage === 'finance'", 1)[0]
    assert "grid-template-columns:" in INDEX
    assert "delivery-detailed-filter-actions" in page
    assert 'v-model="deliveryListFilters.customer_id"' in page
    assert ':options="customerOptions"' in page
    assert '@click="clearDeliveryDetailedFilters">清空详细条件' in page
    assert '@click="resetDeliveryListFilters()">清空全部' not in page
    assert ".delivery-detailed-filterbar > .select { position:relative; z-index:1; }" in INDEX
    assert "overflow:visible" in INDEX


@pytest.mark.parametrize(
    ("mode", "expected_rows"),
    (("standard", "1"), ("large", "2")),
)
def test_1920_delivery_filter_controls_do_not_overlap_or_overflow(
    tmp_path: Path, mode: str, expected_rows: str
) -> None:
    output = _render_layout_probe(tmp_path, mode)
    for marker in (
        'data-probe-complete="true"',
        'data-overlaps="false"',
        f'data-row-count="{expected_rows}"',
        'data-clickable="true"',
        'data-readable="true"',
        'data-dropdown-on-top="true"',
        'data-bar-overflow="false"',
        'data-main-overflow="false"',
    ):
        assert marker in output

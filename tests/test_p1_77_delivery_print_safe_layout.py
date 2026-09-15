from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "static" / "delivery-print.html").read_text(encoding="utf-8")


def _css(selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", PAGE, re.S)
    assert match is not None, f"missing CSS selector: {selector}"
    return match.group("body")


def _between(start: str, end: str) -> str:
    start_index = PAGE.index(start)
    end_index = PAGE.index(end, start_index)
    return PAGE[start_index:end_index]


def test_physical_profile_stays_driver_managed_while_content_is_215mm() -> None:
    assert "--paper-width: 241mm" in PAGE
    assert "--content-safe-width: 215mm" in PAGE
    assert "width: var(--paper-width)" in _css(".sheet")
    assert "width: var(--content-safe-width)" in _css(".print-safe-area")
    assert "margin: 0 auto" in _css(".print-safe-area")
    assert "size: auto" in _css("@page")
    assert "size: 215mm" not in PAGE
    assert "paper_width_mm: 241" in PAGE
    assert "driver_managed" in PAGE


def test_every_customer_visible_block_is_inside_the_safe_area() -> None:
    template = _between('<template id="sheetTemplate">', "</template>")
    assert template.count('class="print-safe-area"') == 1
    safe_start = template.index('class="print-safe-area"')
    safe_end = template.rindex("</div>")
    for marker in (
        'data-field="senderCompanyName"',
        'class="meta"',
        'data-field="itemRows"',
        'data-field="remarkNotes"',
        'class="signatures"',
        'data-field="pageNumber"',
        'data-field="createdAt"',
    ):
        position = template.index(marker)
        assert safe_start < position < safe_end


def test_name_and_spec_are_real_columns_with_larger_detail_text() -> None:
    header = _between("<thead>", "</thead>")
    assert "<th>产品名称</th>" in header
    assert "<th>规格（mm）</th>" in header
    assert "产品名称 / 规格" not in header
    assert "font-size: 13px" in _css("table")
    assert "font-size: 12px" in _css(".product-code")
    assert "font-size: 12px" in _css(".product-name.long-name")
    assert 'class="product-specification"' in PAGE
    assert 'class="product-spec"' not in PAGE
    assert '<col style="width:24%">' in PAGE
    assert '<col style="width:16%">' in PAGE
    assert '<col style="width:4%">' in PAGE
    assert "<col" in PAGE
    assert header.count("<th>") == 8


def test_footer_matches_confirmed_two_signature_and_summary_contract() -> None:
    footer = _between('<footer class="print-footer">', "</footer>")
    assert "本页数量：" in footer
    assert "总数：" in footer
    assert "计价主件总数" not in PAGE
    assert "实际货物总数" not in PAGE
    assert "pricedQuantity" not in PAGE
    assert "经手人" not in PAGE
    assert "送货人：" in footer
    assert "收货单位(签章)：" in footer
    assert footer.count('class="signature-item') == 2
    assert 'class="footer-meta"' in footer
    assert footer.index('data-field="pageNumber"') < footer.index(
        'data-field="createdAt"'
    )
    for reminder in ("白联存档", "红联客户", "黄联回单"):
        assert reminder in footer


def test_customer_remark_wraps_and_internal_pricing_note_is_not_printed() -> None:
    assert "white-space: pre-wrap" in _css(".remark-note")
    assert "overflow-wrap: anywhere" in _css(".remark-notes")
    assert 'const customerRemark = String(item.remarks || "").trim()' in PAGE
    assert "套内子件，不单独计价" not in PAGE
    assert "internal_remark" not in PAGE
    assert "warehouse" not in _between("function populateSheet", "function renderDelivery")


def test_pagination_measures_safe_area_including_remarks_and_footer() -> None:
    render = _between("function renderDelivery", "const printButton")
    assert 'probe.querySelector(".print-safe-area")' in render
    assert "scrollHeight <=" in render
    assert "clientHeight + 1" in render
    assert "populateSheet(probe" in render
    assert "if (!pageFits([entry]))" in render
    assert "currentPage.length && !pageFits(candidate)" in render

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _delivery_page_section() -> str:
    start = INDEX.index("activePage === 'deliveries'")
    end = INDEX.index("activePage === 'finance'", start)
    return INDEX[start:end]


def test_each_delivery_keeps_one_summary_row_and_expands_a_columnar_detail_table() -> None:
    page = _delivery_page_section()

    assert '<template v-for="row in deliveries" :key="row.id">' in page
    assert "toggleDeliveryListDetail(row)" in page
    assert "expandedDeliveryRows[row.id]" in page
    assert "查看明细（" in page
    assert 'class="delivery-list-detail-row"' in page
    assert '<td colspan="9">' in page

    for heading in ("存货编码", "产品名称", "产品尺寸", "数量"):
        assert f">{heading}</th>" in page

    # The summary row must not render one block per item. Product fields belong
    # to the expanded detail table where each business meaning has its own column.
    summary_start = page.index('<template v-for="row in deliveries" :key="row.id">')
    detail_start = page.index('class="delivery-list-detail-row"', summary_start)
    summary = page[summary_start:detail_start]
    assert 'v-for="item in row.items"' not in summary
    assert "item.product_code" not in summary
    assert "item.product_name" not in summary


def test_delivery_detail_keeps_saved_components_in_the_same_clear_columns() -> None:
    page = _delivery_page_section()
    detail_start = page.index('class="delivery-list-detail-row"')
    detail = page[detail_start:]

    assert "deliverySavedComponentLines(item)" in detail
    assert "套内子件" in detail
    assert "component.component_code" in detail
    assert "component.component_name" in detail
    assert "component.specification" in detail


def test_delivery_detail_inline_javascript_is_syntax_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-02-delivery-detail-expand.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

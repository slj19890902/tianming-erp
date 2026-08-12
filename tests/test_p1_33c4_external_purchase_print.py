from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PAGE = (ROOT / "static" / "external-purchase-print.html").read_text(
    encoding="utf-8"
)
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_external_purchase_print_page_is_registered_and_read_only() -> None:
    assert 'route.path == "/external-purchase-print.html"' in MAIN
    assert '"/external-purchase-print.html"' in MAIN
    assert "/api/external-packaging-purchases/${encodeURIComponent(id)}/print" in PAGE
    assert 'credentials:"include"' in PAGE
    assert "window.print()" in PAGE
    for write_method in ('method:"POST"', 'method:"PUT"', 'method:"PATCH"', 'method:"DELETE"'):
        assert write_method not in PAGE
    assert "自动发送" not in PAGE


def test_order_detail_and_confirmation_open_the_same_existing_purchase() -> None:
    assert "external_packaging_purchase_summary.purchase_orders?.length" in INDEX
    assert ":href=\"`/external-purchase-print.html?id=${purchase.id}`\"" in INDEX
    assert "{{ purchase.purchase_number }} · 打印" in INDEX
    assert "打印采购单" in INDEX
    assert INDEX.count("/external-purchase-print.html?id=${purchase.id}") == 2


def test_print_layout_repeats_header_and_avoids_broken_rows() -> None:
    assert "thead{display:table-header-group}" in PAGE
    assert "break-inside:avoid" in PAGE
    assert "page-break-inside:avoid" in PAGE
    assert "size:A4 portrait" in PAGE
    for allowed_label in ("供应商", "采购单号", "确认日期", "规格", "数量"):
        assert allowed_label in PAGE
    for forbidden_label in (
        "客户",
        "客户单号",
        "订单号",
        "交货日期",
        "供应商产品代码",
        "供应商采购成本",
        "货品金额",
        "税额",
        "含税合计",
        "冻结报价条款",
        "MOQ",
        "运费",
        "打样费",
        "版费",
        "刀模费",
        "图纸",
    ):
        assert forbidden_label not in PAGE


def test_external_purchase_print_inline_javascript_passes_node_check(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        source
        for source in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", PAGE, flags=re.DOTALL
        )
        if source.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "external-purchase-print.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

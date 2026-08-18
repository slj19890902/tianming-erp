from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = PROJECT_ROOT / "static" / "delivery-print.html"


def _source() -> str:
    return PRINT_PAGE.read_text(encoding="utf-8")


def _css_block(source: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", source, re.S)
    assert match is not None, f"missing CSS selector: {selector}"
    return match.group("body")


def test_header_and_footer_remove_unneeded_dot_matrix_rules() -> None:
    source = _source()

    for selector in (
        ".factory-address",
        ".meta-value",
        ".remark-notes",
        ".footer-detail",
    ):
        assert "border-top" not in _css_block(source, selector)
        assert "border-bottom" not in _css_block(source, selector)

    assert "border-bottom: 1px solid #111" not in _css_block(
        source, ".signature-line"
    )
    assert "border-bottom: 1px solid #111" in _css_block(
        source, ".receiver-signature .signature-line"
    )


def test_signature_fields_share_one_row_in_requested_order() -> None:
    source = _source()
    signatures = re.search(
        r'<section class="signatures">(?P<body>.*?)</section>',
        source,
        re.S,
    )
    assert signatures is not None
    body = signatures.group("body")

    assert body.index("送货人：") < body.index("收货单位(签章)：")
    assert "经手人：" not in body
    assert body.count('class="signature-line"') == 1
    assert "grid-template-columns: 1fr 1.8fr" in source


def test_print_fonts_are_one_step_larger_without_adding_a_blank_page() -> None:
    source = _source()

    assert "font-size: 21px" in _css_block(source, ".company")
    assert "font-size: 18px" in _css_block(source, ".document-title")
    assert "font-size: 12px" in _css_block(source, ".meta")
    assert "font-size: 13px" in _css_block(source, "table")
    assert "font-size: 13px" in _css_block(source, ".total")
    assert "font-size: 11px" in _css_block(source, ".signatures")

    assert "height: calc(var(--paper-height) - 0.5mm)" in source
    assert "page-break-inside: avoid" in source
    assert ".sheet:not(:last-child)" in source
    assert ".sheet:last-child" in source
    assert "display: flex" in _css_block(source, ".sheet")
    assert "flex-direction: column" in _css_block(source, ".sheet")
    assert "margin-top: auto" in _css_block(source, ".print-footer")
    assert (
        "height: calc(var(--paper-height) - 0.5mm)"
        in _css_block(source, ".sheet")
    )


def test_dot_matrix_print_uses_driver_managed_physical_orientation() -> None:
    source = _source()

    assert "size: auto" in _css_block(source, "@page")
    assert "size: 241mm 139.5mm" not in source
    assert "pageStyle.textContent" not in source
    assert 'data-field="paperGuide"' in source
    assert "ERP 已自动应用" in source
    assert "EPSON SK820" in source
    assert "driver_managed" in source
    assert "不要交换宽高" in source
    assert "再次旋转" in source
    assert "每张纸打印 1 页" in source
    assert "天明ERP送货单-" not in source


def test_delivery_print_inline_javascript_is_syntactically_valid(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the print-page contract test"

    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            _source(),
            re.DOTALL,
        )
        if script.strip()
    ]
    assert len(scripts) == 1

    target = tmp_path / "delivery-print-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_print_page_keeps_auth_and_network_errors_inside_print_tab() -> None:
    source = _source()

    assert "open_token" in source
    assert "BroadcastChannel" in source
    assert 'type: "ready"' in source
    assert 'type === "activate"' in source
    assert 'type === "abort"' in source
    assert "登录已失效，请返回系统登录后重新打印" in source
    assert "无法连接 ERP 服务" in source
    assert "window.opener" not in source
    assert "window.opener.location" not in source
    assert "window.location = '/'" not in source


def test_printed_product_name_and_specification_use_separate_columns() -> None:
    source = _source()

    assert "产品名称 / 规格" not in source
    assert "<th>产品名称</th>" in source
    assert "<th>规格</th>" in source
    assert 'class="product-specification"' in source
    assert 'class="product-spec"' not in source
    assert 'item.specification || ""' in source
    assert "规格未登记" in source

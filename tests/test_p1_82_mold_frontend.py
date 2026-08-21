from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _inline_scripts(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    return [
        match.group(1)
        for match in re.finditer(
            r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", source, re.I
        )
        if match.group(1).strip()
    ]


def test_mold_editor_uses_three_fields_multi_customer_and_stable_save_contract() -> None:
    source = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
    assert 'id="moldCustomerCandidates"' in source
    assert 'id="moldPrimaryCustomer1"' in source
    assert 'id="moldPrimaryCustomer2"' in source
    assert 'id="moldLabelName"' in source
    assert 'id="moldChineseShortName"' in source
    assert "pinyin-pro-3.26.0.js" in source
    assert "customers:moldCustomerAssociations()" in source
    assert "expected_version:Number($(\"moldVersion\").value)" in source
    assert "idempotency_key:null" in source
    assert '<input id="moldCode" type="hidden">' in source


def test_employee_pages_render_display_name_and_never_render_internal_mold_code() -> None:
    mobile = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
    lookup = (ROOT / "static" / "mobile_mold_lookup.html").read_text(
        encoding="utf-8"
    )
    product = (ROOT / "static" / "mobile_product_live.html").read_text(
        encoding="utf-8"
    )
    production_print = (
        ROOT / "static" / "requisition-production-print.html"
    ).read_text(encoding="utf-8")
    label = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")

    assert 'stationFact("模具名称", task.mold_display_name || task.mold_name' in mobile
    assert "模具编号" not in mobile
    assert "item.display_name || item.mold_name" in mobile
    assert "row.display_name||row.mold_name" in lookup
    assert "mold.display_name||mold.mold_name" in product
    assert "line.mold_display_name || line.mold_name" in production_print
    assert "escapeHtml(line.mold_code)" not in production_print
    assert "模具名称与现场位置" in production_print
    assert "模具标签名称按长度" in label


def test_mold_changed_inline_javascript_is_syntax_valid() -> None:
    files = (
        "warehouse.html",
        "mobile_mold_lookup.html",
        "mobile_mold_live.html",
        "mobile_product_live.html",
        "mobile_erp.html",
        "mold-label.html",
        "requisition-production-print.html",
        "index.html",
    )
    for name in files:
        for index, script in enumerate(_inline_scripts(ROOT / "static" / name), start=1):
            result = subprocess.run(
                ["node", "--check", "-"],
                input=script,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0, f"{name} script {index}: {result.stderr}"

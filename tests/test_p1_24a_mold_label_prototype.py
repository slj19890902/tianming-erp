from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
LABEL = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "mobile_mold_lookup.html").read_text(encoding="utf-8")


def test_mold_label_is_fixed_to_real_40x30_paper() -> None:
    assert "40 × 30 mm（固定）" in LABEL
    assert "--paper-width:40mm" in LABEL
    assert "--paper-height:30mm" in LABEL
    assert "@page{size:40mm 30mm;margin:0}" in LABEL
    assert 'id="paperPreset"' not in LABEL
    assert 'id="orientation"' not in LABEL
    assert 'id="customWidth"' not in LABEL
    assert 'id="customHeight"' not in LABEL
    assert "window.print()" in LABEL
    assert "body,html{width:40mm;height:auto" in LABEL
    assert "body>*:not(#previewContent){display:none!important}" in LABEL
    assert "#previewContent,#labels{display:block!important" in LABEL
    assert ".label{display:flex!important" in LABEL
    assert "40×30 标签样式" in WAREHOUSE
    assert "90×60 标签样式" not in WAREHOUSE
    assert "/mold-label.html?prototype=1" in WAREHOUSE


def test_mold_label_keeps_only_complete_on_label_identification_fields() -> None:
    assert "customer_short_name" in LABEL
    assert "customer_name" in LABEL
    assert "customer_code" in LABEL
    assert "product_code" in LABEL
    assert "product_name" in LABEL
    assert "report_specification" in LABEL
    assert "specification" in LABEL
    assert "shortLocation" in LABEL
    assert "模具号、客户、存货、位置、产品尺寸和片料尺寸" in LABEL
    assert "产品尺寸待完善" in LABEL
    assert "片料待完善" in LABEL
    assert "另 ${total-1} 款" in LABEL
    assert "未绑定常用箱 / 存货编码" in LABEL
    assert "停用" in LABEL
    assert "lookupUrl" not in LABEL
    assert "客户名称" not in LABEL
    assert "客户价格" not in LABEL
    assert "扫码查模具" not in LABEL
    assert "qr_data_url" not in LABEL
    assert 'split("-")[0]' not in LABEL
    assert 'textClass(moldCode,12,16)' in LABEL
    assert "topline.with-status" in LABEL
    assert "customer=product?.customer_short_name||product?.customer_name||product?.customer_code" in LABEL
    for marker in (
        "JSD-61494052",
        "SME-CPN087075",
        "资料待完善",
        "已停用模具",
    ):
        assert marker in LABEL


def test_p1_24a_keeps_authentication_and_role_based_return_links() -> None:
    assert 'prototypeMode?"/api/auth/me"' in LABEL
    assert "onUnauthorized:loginNext" in LABEL
    assert "/api/warehouse/molds/${id}/label" in LABEL
    assert "完整资料仍在 ERP 模具档案查询" in LABEL
    assert 'permissions.includes("warehouse.view")' in MOBILE
    assert 'permissions.includes("orders.view")' in MOBILE
    assert 'href="/warehouse.html"' in MOBILE
    assert 'href="/production"' in MOBILE


def test_p1_24a_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    scripts = [
        match.group(1)
        for match in re.finditer(r"<script>(.*?)</script>", LABEL, re.DOTALL)
        if match.group(1).strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-24a-mold-label-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

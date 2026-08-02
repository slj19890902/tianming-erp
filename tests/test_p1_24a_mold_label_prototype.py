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


def test_p1_24a_exposes_90x60_orientation_and_custom_preview() -> None:
    assert '<option value="90x60">90 × 60 mm</option>' in LABEL
    assert '<option value="custom">自定义尺寸</option>' in LABEL
    assert '<option value="landscape">横向</option>' in LABEL
    assert '<option value="portrait">竖向</option>' in LABEL
    assert "--paper-width:90mm" in LABEL
    assert "--paper-height:60mm" in LABEL
    assert '@page{size:${width}mm ${height}mm;margin:0}' in LABEL
    assert "window.print()" in LABEL
    assert "90×60 标签样式" in WAREHOUSE
    assert "/mold-label.html?prototype=1" in WAREHOUSE


def test_p1_24a_uses_board_size_and_honest_overflow_states() -> None:
    assert "row.report_specification" in LABEL
    assert "片料待完善" in LABEL
    assert "共 ${total} 款，扫码查看全部" in LABEL
    assert "已停用" in LABEL
    assert "lookupUrl" not in LABEL
    assert "客户名称" not in LABEL
    assert "客户价格" not in LABEL
    assert "匿名占位码，不可扫码" in LABEL
    for marker in (
        "单款模具",
        "一模多款",
        "多片料尺寸",
        "资料待完善",
        "已停用模具",
    ):
        assert marker in LABEL


def test_p1_24a_keeps_authentication_and_role_based_return_links() -> None:
    assert 'await api("/api/auth/me")' in LABEL
    assert "/api/warehouse/molds/${id}/label" in LABEL
    assert "正式标签二维码仍进入登录后的模具查询" in LABEL
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

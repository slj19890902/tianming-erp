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
    assert "label_identity" in LABEL
    assert "report_specification" in LABEL
    assert "specification" in LABEL
    assert "shortLocation" in LABEL
    assert "客户名称+模具编号、位置、产品尺寸、片料尺寸和固定二维码" in LABEL
    assert "共 ${total} 款见扫码" not in LABEL
    assert "label_product_specification" in LABEL
    assert "label_report_specification" in LABEL
    assert "qr_data_url" in LABEL
    assert 'class="qr"' in LABEL
    assert "width:13.9mm;height:13.9mm" in LABEL
    assert "await refreshRenderedLabels()" in LABEL
    assert "renderGeneration" in LABEL
    assert "image-rendering:pixelated" in LABEL
    assert "await waitForQrImages()" in LABEL
    assert 'split("-")[0]' not in LABEL
    assert "row.label_identity||row.mold_code" in LABEL
    for marker in (
        "聚晟达61452621",
        "SME-CPN087075",
        "资料待完善",
    ):
        assert marker in LABEL


def test_p1_24a_keeps_authentication_and_role_based_return_links() -> None:
    assert 'prototypeMode?"/api/auth/me"' in LABEL
    assert "onUnauthorized:loginNext" in LABEL
    assert "/api/warehouse/molds/${id}/label" in LABEL
    assert "扫码后登录 ERP 查看实时订单、收料和材料位置" in LABEL
    live = (ROOT / "static" / "mobile_mold_live.html").read_text(
        encoding="utf-8"
    )
    assert '(auth.permissions||[]).includes("warehouse.view")' in live
    assert "/api/warehouse/molds/live/${moldId}" in live
    assert "当前账号无订单与模切任务查看权限" in live


def test_mold_live_page_is_read_only_and_never_caches_business_data() -> None:
    live = (ROOT / "static" / "mobile_mold_live.html").read_text(
        encoding="utf-8"
    )
    assert "/api/warehouse/molds/live/${moldId}" in live
    assert 'cache:"no-store"' in live
    assert "实时只读" in live
    assert "当前没有可显示的材料库位" in live
    assert "movePanel" not in live
    assert "warehouse.execute" not in live
    assert "localStorage" not in live
    assert '/^\\d+$/' in live


def test_mold_private_response_middleware_covers_success_and_error_paths() -> None:
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    middleware = (ROOT / "app" / "middleware" / "mold_private.py").read_text(
        encoding="utf-8"
    )
    assert "from app.middleware.mold_private import MoldPrivateNoStoreMiddleware" in main
    assert "class MoldPrivateNoStoreMiddleware" in middleware
    assert 'path.startswith("/api/warehouse/molds/live/")' in middleware
    assert 'path == "/api/warehouse/molds/labels"' in middleware
    assert 'path.endswith("/label")' in middleware
    for marker in (
        'response.headers["Cache-Control"] = "private, no-store, max-age=0"',
        'response.headers["Pragma"] = "no-cache"',
        'vary.add("Cookie")',
        'response.headers["X-Robots-Tag"] = "noindex, nofollow"',
        'response.headers["Referrer-Policy"] = "no-referrer"',
    ):
        assert marker in middleware


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

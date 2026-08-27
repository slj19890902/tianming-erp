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
    assert ".label{display:grid!important" in LABEL
    assert "--print-x-compensation:2mm" in LABEL
    assert "border:0!important" in LABEL
    assert "translateX(calc(0mm - var(--print-x-compensation)))" in LABEL
    assert "translate(calc(0mm - var(--print-x-compensation)),2mm)" in LABEL
    assert "40×30 标签样式" in WAREHOUSE
    assert "90×60 标签样式" not in WAREHOUSE
    assert "/mold-label.html?prototype=1" in WAREHOUSE


def test_mold_label_keeps_only_complete_on_label_identification_fields() -> None:
    assert "label_identity" in LABEL
    assert "report_specification" in LABEL
    assert "specification" in LABEL
    assert "shortLocation" not in LABEL
    assert "沿80mm长边完整放大排版" in LABEL
    assert "内容与单个模具“打印标签”一致" in LABEL
    assert "只调整业务字段的位置、宽高、字号和对齐" in LABEL
    assert "共 ${total} 款见扫码" not in LABEL
    assert "label_product_specification" in LABEL
    assert "label_report_specification" in LABEL
    assert "label_customer_name" in LABEL
    assert "label_mold_number" in LABEL
    assert "label_flute_type" in LABEL
    assert "qr_data_url" in LABEL
    assert 'class="qr"' in LABEL
    assert "width:13.9mm;height:13.9mm" in LABEL
    assert "await refreshRenderedLabels()" in LABEL
    assert "renderGeneration" in LABEL
    assert "image-rendering:pixelated" in LABEL
    assert "await waitForQrImages()" in LABEL
    assert 'split("-")[0]' not in LABEL
    assert "row.label_customer_name||product?.customer_short_name" in LABEL
    assert 'grid-column:2;grid-row:1/4' in LABEL
    assert 'align-self:end' in LABEL
    assert '<span class="field-key">位置</span>' not in LABEL
    assert 'class="product-flute-row"' in LABEL
    assert 'factRow("片料",board,"","board-row")' in LABEL
    assert '.customer-name{font-size:3.05mm' in LABEL
    assert 'function numberClass(value)' in LABEL
    assert 'length>19?"xxlong":length>15?"xlong":length>11?"long":length>8?"compact"' in LABEL
    assert '.mold-number.compact{font-size:3.85mm' in LABEL
    assert '.mold-number.xxlong{font-size:2.2mm' in LABEL
    assert 'extraClass==="flute"?3:12' in LABEL
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
    assert "扫码后登录 ERP 查看实时信息" in LABEL
    live = (ROOT / "static" / "mobile_mold_live.html").read_text(
        encoding="utf-8"
    )
    assert '(auth.permissions||[]).includes("warehouse.view")' in live
    assert "/api/warehouse/molds/live/${moldId}" in live
    assert "当前账号无订单与模切任务查看权限" in live


def test_mold_live_page_records_only_task_scan_history_and_never_caches() -> None:
    live = (ROOT / "static" / "mobile_mold_live.html").read_text(
        encoding="utf-8"
    )
    assert "/api/warehouse/molds/live/${moldId}" in live
    assert 'cache:"no-store"' in live
    assert "实时读取 ERP" in live
    assert "/scan-events" in live
    assert 'method:"POST"' in live
    assert "本次扫码已登记" not in live
    assert "该记录只表示领模/核对，不会自动开工" in live
    assert 'field("客户订单号",row.order_number)' in live
    assert 'field("存货编码",row.product_code)' in live
    assert 'field("该存货编码订单数量",quantity)' in live
    assert 'field("具体进度状态",taskProgress(row)' in live
    assert "查看待来料任务单其余信息" in live
    assert "扫码生产历史" in live
    assert "orders.length===1&&!scanEvent" in live
    assert "orders.length>1" in live
    assert "系统不会猜测" in live
    assert "production_task_id:productionTaskId" in live
    assert "expected_mold_location_version:latestData.mold.location_version" in live
    assert "idempotency_key:scanSessionKey" in live
    assert "当前没有可显示的材料库位" in live
    assert "movePanel" not in live
    assert "warehouse.execute" not in live
    assert "localStorage" not in live
    assert '/^\\d+$/' in live


def test_mold_live_page_simplifies_material_and_scan_timestamps() -> None:
    live = (ROOT / "static" / "mobile_mold_live.html").read_text(
        encoding="utf-8"
    )
    assert 'function materialCode(value)' in live
    assert '.split("/")[0].trim()' in live
    assert '[row.material_code,row.material_composition]' not in live
    assert 'function formatDateTime(value)' in live
    assert 'timeZone:"Asia/Shanghai"' in live
    assert 'hourCycle:"h23"' in live
    assert 'formatDateTime(row.scanned_at)' in live
    assert 'formatDateTime(data.as_of)' in live
    assert '${h(row.scanned_at)}' not in live
    assert '${h(data.as_of)}' not in live


def test_mold_live_formatters_return_shop_floor_friendly_values(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    live = (ROOT / "static" / "mobile_mold_live.html").read_text(
        encoding="utf-8"
    )
    format_function = re.search(
        r"    function formatDateTime\(value\)\{.*?\n    \}",
        live,
        re.DOTALL,
    )
    material_function = re.search(
        r"    function materialCode\(value\)\{.*?\n    \}",
        live,
        re.DOTALL,
    )
    assert format_function
    assert material_function
    script = tmp_path / "p1-68-mold-live-formatters.js"
    script.write_text(
        "\n".join(
            (
                format_function.group(0),
                material_function.group(0),
                'console.log(formatDateTime("2026-08-17T16:05:45+08:00"));',
                'console.log(materialCode("J616D/140+120+120"));',
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["2026-08-17 16:05", "J616D"]


def test_mold_live_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    live = (ROOT / "static" / "mobile_mold_live.html").read_text(
        encoding="utf-8"
    )
    scripts = [
        match.group(1)
        for match in re.finditer(r"<script>(.*?)</script>", live, re.DOTALL)
        if match.group(1).strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-67-mobile-mold-live-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
        'headers["Cache-Control"] = "private, no-store, max-age=0"',
        'headers["Pragma"] = "no-cache"',
        'vary.add("Cookie")',
        'headers["X-Robots-Tag"] = "noindex, nofollow"',
        'headers["Referrer-Policy"] = "no-referrer"',
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

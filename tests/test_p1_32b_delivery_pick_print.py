from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRINT = (ROOT / "static" / "delivery-pick-print.html").read_text(
    encoding="utf-8"
)
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "mobile_delivery_pick.html").read_text(
    encoding="utf-8"
)


def test_desktop_delivery_row_opens_independent_pick_print_page() -> None:
    assert "openDeliveryPickPrint(row)" in INDEX
    assert "打印拿货单" in INDEX
    assert "/static/delivery-pick-print.html?task_id=" in INDEX
    assert '"_blank", "noopener"' in INDEX


def test_a4_pick_sheet_contains_required_readable_fields_and_pagination() -> None:
    for marker in (
        "A4 预拿货单",
        "客户：",
        "拿货单号：",
        "关联送货单：",
        "计划送货：",
        "打印版本：",
        "打印人/时间：",
        "第 ${pageIndex + 1}/${pageCount} 页",
        "存货编码",
        "产品名称 / 规格",
        "位置",
        "计划拿货",
        "实际拿货",
        "差异",
        "本品合计",
        "拿货人：",
        "复核人：",
        "异常行数：",
    ):
        assert marker in PRINT
    assert "ROWS_PER_PAGE = 18" in PRINT
    assert "整单合计见末页" in PRINT
    assert "pageIndex === pageCount - 1" in PRINT
    assert "page-break-inside: avoid" in PRINT
    assert "size: A4 portrait" in PRINT


def test_print_uses_existing_location_plan_and_never_writes_business_state() -> None:
    assert "/api/delivery-picks/${encodeURIComponent(taskId)}" in PRINT
    assert "task.location_groups" in PRINT
    assert "group.recommended_sequence" in PRINT
    assert "group.lines" in PRINT
    assert 'credentials:"include"' in PRINT
    assert 'request("/api/auth/me")' in PRINT
    for forbidden in (
        'method:"POST"',
        'method: "POST"',
        'method:"PUT"',
        'method: "PUT"',
        'method:"DELETE"',
        'method: "DELETE"',
    ):
        assert forbidden not in PRINT
    assert "纸面填写不改库存、不发货" in PRINT


def test_difference_reentry_is_task_and_print_version_bound() -> None:
    assert "print_version=${encodeURIComponent(task.print_version)}" in PRINT
    assert 'const expectedPrintVersion = initialParams.get("print_version")' in MOBILE
    assert "task.print_version !== expectedPrintVersion" in MOBILE
    assert "纸质拿货单版本已过期" in MOBILE
    assert "/items/${encodeURIComponent(id)}`" in MOBILE
    assert "/complete-planned`" in MOBILE


def test_pick_print_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the print-page contract test"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            PRINT,
            re.DOTALL,
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "delivery-pick-print-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

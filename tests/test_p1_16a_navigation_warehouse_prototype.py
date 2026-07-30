from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "docs" / "P1_16A_NAVIGATION_WAREHOUSE_AUDIT.md"
PROTOTYPE_PATH = (
    ROOT
    / "docs"
    / "prototypes"
    / "p1_16a_navigation_warehouse"
    / "index.html"
)
AUDIT = AUDIT_PATH.read_text(encoding="utf-8")
PROTOTYPE = PROTOTYPE_PATH.read_text(encoding="utf-8")


def test_audit_is_replayed_on_current_chain_with_code_evidence() -> None:
    assert "b3535f938f0042e230b0a874f39713492be0c174" in AUDIT
    assert "`db84v8x9z73`" in AUDIT
    for evidence in (
        "static/index.html:2609-2655, 6239-6255",
        "static/warehouse.html:1261-1281, 1487-1500",
        "app/models/supplier.py:21-103",
        "app/models/product_bom.py:398-490",
        "app/api/requisition.py:4496-4853",
        "app/services/unordered_finished_delivery.py:48-461",
    ):
        assert evidence in AUDIT


def test_prototype_has_exactly_three_directions_and_two_display_modes() -> None:
    direction_buttons = re.findall(
        r"<button[^>]*data-direction=\"([abc])\"[^>]*>",
        PROTOTYPE,
    )
    assert direction_buttons == ["a", "b", "c"]
    assert "方向 A · 轻立体导视" in PROTOTYPE
    assert "方向 B · 楼层卡片" in PROTOTYPE
    assert "方向 C · 空间控制台" in PROTOTYPE

    display_buttons = re.findall(
        r"<button[^>]*data-display=\"([^\"]+)\"[^>]*>",
        PROTOTYPE,
    )
    assert display_buttons == ["standard", "large"]


def test_prototype_is_offline_anonymous_and_cannot_write() -> None:
    forbidden = (
        "fetch(",
        "XMLHttpRequest",
        "axios",
        "/api/",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "<form",
        'type="file"',
        "http://",
        "https://",
    )
    for token in forbidden:
        assert token not in PROTOTYPE

    assert "所有客户、编号、数量、占用率和状态均为离线示例，不代表当前正式数据" in PROTOTYPE
    assert "这是离线原型，不会保存" in PROTOTYPE
    for misleading in ("78 / 116", "78/116", "三楼实时", "当前正式布局"):
        assert misleading not in PROTOTYPE

    for operation in (
        "绑定货物（演示）",
        "移动栈板（演示）",
        "编辑草稿（演示）",
        "预览草稿（演示）",
        "新建受控变更（演示）",
        "查看操作记录（演示）",
    ):
        assert operation in PROTOTYPE


def test_four_business_tabs_and_full_warehouse_drilldown_are_clickable() -> None:
    worktabs = re.findall(
        r"\[\"(orders|requisition|incoming|warehouse)\", \"([^\"]+)\"\]",
        PROTOTYPE,
    )
    assert worktabs == [
        ("orders", "订单"),
        ("requisition", "报料"),
        ("incoming", "来料入库"),
        ("warehouse", "仓库地图"),
    ]

    for contract in (
        'data-depth="floor3"',
        'data-depth="area"',
        'data-depth="floor1"',
        'data-depth="admin"',
        'data-location="${row.code}"',
        "当前位空闲。",
        "匿名栈板 · 示例占用",
        "一楼台账建设中",
        "D2 · 19个地堆位",
        "F1 / F2 / F3 / F4",
    ):
        assert contract in PROTOTYPE


def test_large_mode_and_whole_page_overflow_guards_are_present() -> None:
    assert "overflow-x: hidden" in PROTOTYPE
    assert "grid-template-columns: var(--sidebar) minmax(0, 1fr)" in PROTOTYPE
    assert ".warehouse-layout { display: grid; grid-template-columns: minmax(0, 1fr) var(--rightbar)" in PROTOTYPE
    assert 'document.body.classList.toggle("large", state.display === "large")' in PROTOTYPE
    assert 'body.large { --sidebar: 248px; --rightbar: 330px; --font: 17px; }' in PROTOTYPE
    assert 'document.querySelectorAll("button[data-direction]")' in PROTOTYPE
    assert 'event.target.closest("button[data-direction]")' in PROTOTYPE
    assert 'document.querySelectorAll("[data-direction]")' not in PROTOTYPE
    assert 'event.target.closest("[data-direction]")' not in PROTOTYPE


def test_inline_javascript_is_syntax_valid() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for inline JavaScript syntax checks"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            PROTOTYPE,
            flags=re.DOTALL,
        )
        if script.strip()
    ]
    assert scripts
    for script in scripts:
        result = subprocess.run(
            [node, "--check"],
            input=script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=os.environ.copy(),
            check=False,
        )
        assert result.returncode == 0, result.stderr

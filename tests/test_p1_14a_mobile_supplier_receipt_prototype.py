from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs" / "P1_14A_MOBILE_SUPPLIER_RECEIPT_AUDIT.md"
PROTOTYPE = (
    ROOT
    / "docs"
    / "prototypes"
    / "p1_14a_supplier_receipt_mobile"
    / "index.html"
)
AUDIT_SOURCE = AUDIT.read_text(encoding="utf-8")
PROTOTYPE_SOURCE = PROTOTYPE.read_text(encoding="utf-8")


def test_audit_uses_current_base_and_current_partial_success_semantics() -> None:
    assert "229377a90050bb96b6c2f77203ac631ae8b0a911" in AUDIT_SOURCE
    assert "`db84v8x9z73`" in AUDIT_SOURCE
    assert "逐行处理、允许部分成功" in AUDIT_SOURCE
    assert "每行使用独立 savepoint" in AUDIT_SOURCE
    assert "不是“整张送货单全部成功或全部回滚”的原子接口" in AUDIT_SOURCE

    for evidence in (
        "supplier_master_records",
        "requisition_item_bom_sources",
        "operation_logs",
        "requisition_holds",
        "app/services/secure_uploads.py",
    ):
        assert evidence in AUDIT_SOURCE


def test_prototype_is_anonymous_offline_and_has_no_write_channel() -> None:
    forbidden = (
        "fetch(",
        "XMLHttpRequest",
        "axios",
        "/api/",
        "FileReader",
        "localStorage",
        "sessionStorage",
        'type="file"',
        "<form",
        "http://",
        "https://",
    )
    for token in forbidden:
        assert token not in PROTOTYPE_SOURCE

    assert "载入匿名照片演示" in PROTOTYPE_SOURCE
    assert "开始识别演示" in PROTOTYPE_SOURCE
    assert "纯演示 · 不上传 · 不入库" in PROTOTYPE_SOURCE
    assert "完成演示（不入库）" in PROTOTYPE_SOURCE
    assert "只保存在当前页面内存中" in PROTOTYPE_SOURCE


def test_prototype_does_not_claim_atomic_or_completed_formal_receipt() -> None:
    for misleading in (
        "确认整张收料",
        "整张收料已确认",
        "正式实收",
        "整张原子提交成功",
        "不允许只成功其中几款",
    ):
        assert misleading not in PROTOTYPE_SOURCE

    assert "当前 ERP 批量收料是逐行处理，可能部分成功" in PROTOTYPE_SOURCE
    assert "查看提交前汇总（演示）" in PROTOTYPE_SOURCE
    assert "核对演示完成，没有上传照片，也没有增加库存" in PROTOTYPE_SOURCE


def test_mobile_contract_and_clickable_demo_content_are_present() -> None:
    assert 'name="viewport"' in PROTOTYPE_SOURCE
    assert "width: min(100%, 430px)" in PROTOTYPE_SOURCE
    assert "overflow-x: hidden" in PROTOTYPE_SOURCE
    assert "@media (max-width: 360px)" in PROTOTYPE_SOURCE

    for key in ("short", "surplus", "missing", "candidate"):
        assert f'data-exception="{key}"' in PROTOTYPE_SOURCE
        assert f'data-resolve="{key}"' in PROTOTYPE_SOURCE

    for text in (
        "少到 20 张",
        "多到 20 张",
        "现场未送",
        "找到 2 条",
        "疑似重复",
        "照片模糊",
        "近期来料详情（只读）",
        "不提供生产、移动库存、扣料或完工按钮",
    ):
        assert text in PROTOTYPE_SOURCE


def test_inline_javascript_is_syntax_valid() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for inline JavaScript syntax checks"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            PROTOTYPE_SOURCE,
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

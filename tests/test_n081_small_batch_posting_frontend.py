from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
POSTING_PANEL = WAREHOUSE.split(
    '<div id="onboardingPostingPanel"',
    1,
)[1].split('<form id="onboardingLineEditor"', 1)[0]
POSTING_FUNCTION = WAREHOUSE.split(
    "async function postInventoryOnboardingBatch(){",
    1,
)[1].split("function stocktakePick(", 1)[0]


def test_posting_panel_is_one_button_without_extra_input() -> None:
    assert POSTING_PANEL.count('id="postOnboardingBatch"') == 1
    assert 'id="onboardingPostingSummary"' in POSTING_PANEL
    assert "小批正式入账" in POSTING_PANEL
    assert ">正式入账<" in POSTING_PANEL
    for forbidden in (
        "<input",
        "<textarea",
        "确认短语",
        "回滚",
        "输入原因",
        "二次确认",
    ):
        assert forbidden not in POSTING_PANEL


def test_posting_button_is_review_permission_and_frozen_batch_gated() -> None:
    assert (
        'const canReviewStocktakes=()=>'
        'hasPermission("warehouse.stocktake.review")'
    ) in WAREHOUSE
    assert (
        'if(!canReviewStocktakes())document.querySelectorAll('
        '".onboarding-post-only").forEach'
    ) in WAREHOUSE
    assert (
        '$("postOnboardingBatch").onclick='
        "postInventoryOnboardingBatch"
    ) in WAREHOUSE
    assert (
        "!canReviewStocktakes()||!batch||"
        'batch.status!=="submitted"||batch.posting||'
        "!batch.postable_summary?.can_post"
    ) in POSTING_FUNCTION
    assert (
        'classList.toggle("hidden",Boolean(posting)||'
        "!canReviewStocktakes())"
    ) in WAREHOUSE


def test_posting_is_one_bodyless_request_without_confirmation_or_reason() -> None:
    assert (
        "`${INVENTORY_ONBOARDING_API}/batches/"
        "${encodeURIComponent(batch.id)}/post`"
    ) in POSTING_FUNCTION
    assert '{method:"POST"}' in POSTING_FUNCTION
    assert "body:" not in POSTING_FUNCTION
    for forbidden in (
        "confirm(",
        "prompt(",
        "confirmed",
        "confirmation_phrase",
        "reason",
        "idempotency_key",
    ):
        assert forbidden not in POSTING_FUNCTION
    assert 'toast("小批库存已正式入账")' in POSTING_FUNCTION


def test_old_b2_pilot_friction_and_fixed_pallet_counts_are_removed() -> None:
    for forbidden in (
        "inventoryPilot",
        "INVENTORY_PILOT",
        "N081-B2 隔离试盘",
        "仅在隔离UAT执行N081-B2小范围试盘",
        "回滚N081-B2隔离试盘并保留审计",
        "3～5 个成品栈板",
        "2～3 个半成品栈板",
    ):
        assert forbidden not in WAREHOUSE
    assert "postable_summary?.can_post" in WAREHOUSE
    assert "正式入账（${Number(summary.line_count||0)} 行）" in WAREHOUSE


def test_warehouse_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        WAREHOUSE,
        flags=re.DOTALL | re.IGNORECASE,
    )
    inline = "\n".join(script for script in scripts if script.strip())
    output = tmp_path / "warehouse-inline-n081-posting.js"
    output.write_text(inline, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

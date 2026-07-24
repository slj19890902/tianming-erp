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
CONFIRM_FUNCTION = WAREHOUSE.split(
    "async function confirmInventoryOnboardingBatch(){",
    1,
)[1].split("function stocktakePick(", 1)[0]


def test_page_has_one_plain_confirm_button_and_success_panel() -> None:
    assert WAREHOUSE.count('id="confirmOnboardingBatch"') == 1
    assert 'id="submitOnboardingBatch"' not in WAREHOUSE
    assert 'id="postOnboardingBatch"' not in WAREHOUSE
    assert 'id="onboardingPostingSummary"' in POSTING_PANEL
    assert "盘点库存已入库" in POSTING_PANEL
    assert ">确认盘点入库<" in WAREHOUSE
    for forbidden in (
        "<input",
        "<textarea",
        "确认短语",
        "回滚",
        "输入原因",
        "二次确认",
    ):
        assert forbidden not in POSTING_PANEL


def test_confirm_button_keeps_submit_and_review_permission_gates() -> None:
    assert (
        'const canReviewStocktakes=()=>'
        'hasPermission("warehouse.stocktake.review")'
    ) in WAREHOUSE
    assert (
        'if(!canReviewStocktakes())document.querySelectorAll('
        '".onboarding-post-only").forEach'
    ) in WAREHOUSE
    assert (
        '$("confirmOnboardingBatch").onclick='
        "confirmInventoryOnboardingBatch"
    ) in WAREHOUSE
    assert (
        'const frozen=batch.status==="submitted";'
        "if(frozen){if(!canReviewStocktakes())return}"
        "else if(!canEditInventoryOnboarding()||!canReviewStocktakes())return"
    ) in CONFIRM_FUNCTION
    assert (
        "canConfirm=frozen?canReviewStocktakes():"
        "(canEdit&&canReviewStocktakes())"
    ) in WAREHOUSE


def test_confirm_submits_then_posts_without_user_confirmation_or_reason() -> None:
    assert CONFIRM_FUNCTION.index("/submit") < CONFIRM_FUNCTION.index("/post")
    assert (
        "dry_run_fingerprint:batch.dry_run_fingerprint,"
        "idempotency_key:inventoryOnboardingSubmitKey(batch),confirmed:true"
    ) in CONFIRM_FUNCTION
    post_call = CONFIRM_FUNCTION.split("/post", 1)[1]
    assert '{method:"POST"}' in post_call
    assert "body:" not in post_call.split(
        "useInventoryOnboardingActionResponse(posted)",
        1,
    )[0]
    for forbidden in (
        "confirm(",
        "prompt(",
        "confirmation_phrase",
        "reason",
    ):
        assert forbidden not in CONFIRM_FUNCTION
    assert (
        'toast("盘点库存已入库，订单现在可以查询和使用")'
        in CONFIRM_FUNCTION
    )


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
    assert "确认盘点入库（${readyCount} 条）" in WAREHOUSE


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

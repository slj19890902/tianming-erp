from __future__ import annotations

from pathlib import Path

from app.api.materials import PriceAdjustApplyRequest


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_price_adjust_request_accepts_missing_blank_or_null_reason() -> None:
    base = {
        "supplier_name": "测试供应商",
        "adjust_percent": "5",
        "expected_versions": {1: 1},
    }
    assert PriceAdjustApplyRequest(**base).change_reason is None
    assert PriceAdjustApplyRequest(**base, change_reason="   ").change_reason is None
    assert PriceAdjustApplyRequest(**base, change_reason=None).change_reason is None


def test_price_adjust_ui_keeps_preview_and_one_apply_confirmation_without_reason() -> None:
    assert "预览调价" in INDEX
    assert "确认应用调价" in INDEX
    assert "priceAdjustPreviewValid" in INDEX
    assert "expected_versions" in INDEX
    assert "confirmation_tokens" in INDEX
    assert "将先备份数据库并写入价格历史" in INDEX
    assert "请填写本次批量调价原因" not in INDEX
    assert "priceAdjustForm.change_reason" not in INDEX

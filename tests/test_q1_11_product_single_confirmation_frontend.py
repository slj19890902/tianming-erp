from __future__ import annotations

from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_common_box_update_uses_read_only_preview_inside_one_click_save() -> None:
    assert "/api/master/products/${encodeURIComponent(productId)}/update-preview" in INDEX
    assert "prepareProductOneClickSave" in INDEX
    assert "confirmation_token:data.confirmation_token || undefined" in INDEX
    assert "const preflight = await this.prepareProductOneClickSave()" in INDEX
    assert "masterOptions = preflight.options" in INDEX
    assert "prepareProductChangeConfirmation" not in INDEX


def test_common_box_save_has_no_reason_acknowledgement_or_confirmation_dialog() -> None:
    preflight = INDEX.split("async prepareProductOneClickSave()", 1)[1].split(
        "attachMasterUpdateMetadata", 1
    )[0]
    assert "masterChangeConfirm" not in preflight
    assert "confirm(" not in preflight
    assert 'v-model.trim="masterChangeConfirm.reason"' not in INDEX
    assert "masterChangeConfirm.acknowledged" not in INDEX
    assert "修改原因 *" not in INDEX
    assert "我已核对异常修改" not in INDEX


def test_common_box_one_click_save_submits_token_without_fabricated_reason() -> None:
    preflight = INDEX.split("async prepareProductOneClickSave()", 1)[1].split(
        "attachMasterUpdateMetadata", 1
    )[0]
    assert "change_reason" not in preflight
    assert (
        "if (options?.change_reason !== undefined) "
        "payload.change_reason = options.change_reason;"
        in INDEX
    )
    assert "payload.change_reason = \"确认\"" not in INDEX
    assert "payload.change_reason = \"同意\"" not in INDEX


def test_version_history_keeps_existing_historical_reason_visible() -> None:
    assert "item.change_reason || item.reason || '-'" in INDEX

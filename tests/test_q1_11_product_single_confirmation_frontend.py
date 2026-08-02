from __future__ import annotations

from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_common_box_update_uses_read_only_preview_before_single_confirmation() -> None:
    assert "/api/master/products/${encodeURIComponent(productId)}/update-preview" in INDEX
    assert "prepareProductChangeConfirmation" in INDEX
    assert "confirmationToken:data.confirmation_token || null" in INDEX
    assert "warnings:Array.isArray(data.warnings)" in INDEX
    assert "await this.prepareProductChangeConfirmation(changes)" in INDEX


def test_common_box_confirmation_has_no_reason_or_acknowledgement_gate() -> None:
    assert "masterSingleConfirmProductUpdate" in INDEX
    assert "masterSingleConfirmNoReasonUpdate" in INDEX
    assert (
        'v-if="!masterSingleConfirmNoReasonUpdate"'
        in INDEX
    )
    assert (
        'v-if="masterChangeConfirm.requireAcknowledgement && '
        '!masterSingleConfirmNoReasonUpdate"'
        in INDEX
    )
    assert "const singleNoReasonUpdate = this.masterSingleConfirmNoReasonUpdate;" in INDEX
    assert (
        "if (!singleNoReasonUpdate && !String(state.reason || \"\").trim()) return false;"
        in INDEX
    )
    assert (
        "if (state.requireAcknowledgement && !singleNoReasonUpdate && "
        "!state.acknowledged) return false;"
        in INDEX
    )


def test_common_box_confirm_submits_token_without_fabricated_reason() -> None:
    assert (
        "if (!this.masterSingleConfirmNoReasonUpdate) "
        "options.change_reason = String(state.reason || \"\").trim();"
        in INDEX
    )
    assert (
        "if (options?.change_reason !== undefined) "
        "payload.change_reason = options.change_reason;"
        in INDEX
    )
    assert "payload.change_reason = \"确认\"" not in INDEX
    assert "payload.change_reason = \"同意\"" not in INDEX


def test_version_history_keeps_existing_historical_reason_visible() -> None:
    assert "item.change_reason || item.reason || '-'" in INDEX

from __future__ import annotations

from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_customer_update_previews_before_one_confirmation() -> None:
    assert "prepareCustomerChangeConfirmation" in INDEX
    assert "/api/master/customers/${encodeURIComponent(customerId)}/update-preview" in INDEX
    assert 'else if (masterEntity === "customer") await this.prepareCustomerChangeConfirmation(changes);' in INDEX
    assert 'entity:"customer"' in INDEX
    assert "confirmationToken:data.confirmation_token || null" in INDEX
    assert "requireAcknowledgement:(Array.isArray(data.warnings) ? data.warnings.length : 0) > 0" in INDEX


def test_customer_update_confirmation_does_not_request_or_fabricate_reason() -> None:
    assert 'state.entity === "customer"' in INDEX
    assert "masterSingleConfirmNoReasonUpdate" in INDEX
    assert 'reasonLabel:false' in INDEX
    assert "if (reason) mutationPayload.change_reason = reason;" in INDEX
    assert 'change_reason:"确认"' not in INDEX
    assert 'change_reason:"同意"' not in INDEX


def test_customer_status_and_delete_keep_one_visible_confirmation() -> None:
    assert 'confirm(`确认停用客户“${row.name}”吗？`)' in INDEX
    assert 'confirm(`确认${action}客户“${row.name}”吗？`)' in INDEX
    assert 'url:`/api/master/customers/${row.id}`,row,reasonLabel:false' in INDEX
    assert 'body:{is_active:!row.is_active},reasonLabel:false' in INDEX

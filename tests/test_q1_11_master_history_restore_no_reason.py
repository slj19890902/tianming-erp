from __future__ import annotations

from app.api.master_data_versions import RestorePayload


def test_restore_payload_accepts_missing_or_blank_reason_but_requires_token() -> None:
    payload = RestorePayload(expected_version=2, confirmation_token="token")
    assert payload.reason is None
    blank = RestorePayload(
        expected_version=2,
        reason="   ",
        confirmation_token="token",
    )
    assert blank.reason is None

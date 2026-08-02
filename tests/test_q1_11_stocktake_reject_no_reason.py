from app.api.stocktake import StocktakeRejectRequest


def test_reject_payload_accepts_missing_blank_and_legacy_reason() -> None:
    assert StocktakeRejectRequest(idempotency_key="reject-1").reason is None
    assert StocktakeRejectRequest(idempotency_key="reject-2", reason="   ").reason is None
    assert StocktakeRejectRequest(
        idempotency_key="reject-3", reason="库位记录有误"
    ).reason == "库位记录有误"

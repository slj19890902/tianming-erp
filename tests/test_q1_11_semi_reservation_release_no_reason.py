from pathlib import Path

from app.api.warehouse import SemiReleasePayload


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_API = (ROOT / "app" / "api" / "warehouse.py").read_text(encoding="utf-8")
SEMI_SERVICE = (ROOT / "app" / "services" / "semi_finished_inventory.py").read_text(
    encoding="utf-8"
)


def test_release_payload_accepts_missing_blank_and_legacy_reason() -> None:
    assert SemiReleasePayload(expected_version=1, idempotency_key="release-1").release_reason is None
    assert SemiReleasePayload(
        expected_version=1, release_reason="", idempotency_key="release-2"
    ).release_reason == ""
    assert SemiReleasePayload(
        expected_version=1, release_reason="客户取消", idempotency_key="release-3"
    ).release_reason == "客户取消"


def test_release_keeps_customer_version_balance_idempotency_and_audit() -> None:
    assert "_require_reservation_customer_access" in WAREHOUSE_API
    assert "db.rollback()" in WAREHOUSE_API
    assert "_idempotent_mutation(" in SEMI_SERVICE
    assert "has_production_completion_facts" in SEMI_SERVICE
    assert "quantity > remaining" in SEMI_SERVICE
    assert "lot.version != expected_version" in SEMI_SERVICE
    assert "InventoryLot.version == expected_version" in SEMI_SERVICE
    assert "InventoryLot.quantity_reserved >= quantity" in SEMI_SERVICE
    assert 'movement_type="release_reserve"' in SEMI_SERVICE
    assert 'or "释放半成品库存预占（系统记录）"' in SEMI_SERVICE
    assert "reservation.release_reason = reason" in SEMI_SERVICE

from pathlib import Path

from app.api.warehouse import ReleaseReservationPayload


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_API = (ROOT / "app" / "api" / "warehouse.py").read_text(encoding="utf-8")
WAREHOUSE_SERVICE = (ROOT / "app" / "services" / "warehouse_inventory.py").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method(source: str, name: str, next_name: str) -> str:
    return source.split(f"async {name}", 1)[1].split(f"async {next_name}", 1)[0]


def test_release_payload_accepts_missing_blank_and_legacy_reason() -> None:
    assert ReleaseReservationPayload(idempotency_key="release-123").release_reason is None
    assert ReleaseReservationPayload(
        release_reason="", idempotency_key="release-456"
    ).release_reason == ""
    assert ReleaseReservationPayload(
        release_reason="客户取消", idempotency_key="release-789"
    ).release_reason == "客户取消"


def test_frontend_uses_one_impact_confirmation_without_reason_prompt() -> None:
    method = _method(
        INDEX,
        "releaseFinishedInventoryReservation(reservation)",
        "deleteCurrentOrderItem()",
    )
    assert method.count("confirm(") == 1
    assert "prompt(" not in method
    assert "release_reason" not in method
    assert "idempotency_key:createIdempotencyKey()" in method
    assert "数量会回到可用库存" in method


def test_backend_keeps_inventory_safety_and_automatic_audit() -> None:
    assert 'or "取消成品库存抵扣（系统记录）"' in WAREHOUSE_SERVICE
    assert "has_production_completion_facts" in WAREHOUSE_SERVICE
    assert 'item.requisition_status != "未报料"' in WAREHOUSE_SERVICE
    assert "DeliveryItem.order_item_id == item.id" in WAREHOUSE_SERVICE
    assert "InventoryLot.version == lot.version" in WAREHOUSE_SERVICE
    assert "InventoryLot.quantity_reserved >= quantity" in WAREHOUSE_SERVICE
    assert 'movement_type="release_reserve"' in WAREHOUSE_SERVICE
    assert "reservation.release_reason = reason" in WAREHOUSE_SERVICE
    assert "_require_reservation_customer_access" in WAREHOUSE_API
    assert "db.rollback()" in WAREHOUSE_API

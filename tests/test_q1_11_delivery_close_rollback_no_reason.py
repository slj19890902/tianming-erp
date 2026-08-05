from pathlib import Path

from app.api.deliveries import ForceCloseRequest


ROOT = Path(__file__).resolve().parents[1]
DELIVERIES = (ROOT / "app" / "api" / "deliveries.py").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_force_close_payload_accepts_missing_blank_and_legacy_reason() -> None:
    assert ForceCloseRequest().reason is None
    assert ForceCloseRequest(reason="   ").reason is None
    assert ForceCloseRequest(reason="客户不再需要尾数").reason == "客户不再需要尾数"


def test_force_close_keeps_scope_lock_stock_gate_atomic_update_and_audit() -> None:
    assert "_require_order_item_customer_access" in DELIVERIES
    assert "lock_order_rows_for_production_transition" in DELIVERIES
    assert "_has_active_production_stock_reservation" in DELIVERIES
    assert "OrderItem.is_force_closed.is_(False)" in DELIVERIES
    assert "OrderItem.delivered_quantity < OrderItem.quantity" in DELIVERIES
    assert 'action="FORCE_CLOSE_ORDER_ITEM"' in DELIVERIES
    assert '"订单未送尾数强制结案（系统记录）"' in DELIVERIES
    assert "db.rollback()" in DELIVERIES


def test_delivery_cancel_already_uses_one_confirmation_without_reason() -> None:
    method = INDEX.split("async cancelDelivery(row)", 1)[1].split(
        "async cancelReceipt(row)", 1
    )[0]
    assert method.count("confirm(") == 1
    assert "prompt(" not in method
    assert "若已有回单或已进入对账，系统会拒绝取消" in method
    assert "axios.put(`/api/deliveries/${deliveryId}/cancel`)" in method

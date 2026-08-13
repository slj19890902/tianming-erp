from __future__ import annotations

from pathlib import Path

from app.api.orders import OrderStatusRequest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
ORDERS = (ROOT / "app" / "api" / "orders.py").read_text(encoding="utf-8")


def test_manual_order_status_accepts_missing_blank_or_null_remark() -> None:
    assert OrderStatusRequest(status="dead").remark is None
    assert OrderStatusRequest(status="closed", remark="").remark == ""
    assert OrderStatusRequest(status="archived", remark=None).remark is None


def test_order_status_ui_uses_one_confirmation_without_reason_prompt() -> None:
    assert "标记为${label}必须填写备注原因" not in INDEX
    assert "`${label}必须填写备注`" not in INDEX
    assert "系统会自动记录操作人、时间和原状态" in INDEX
    assert 'axios.put(`/api/orders/${order.id}/status`,{status})' in INDEX


def test_group_status_confirms_once_and_refreshes_once() -> None:
    assert "当前订单组的 ${orders.length} 张订单" in INDEX
    assert "{confirmed:true,reload:false,quiet:true}" in INDEX
    assert "订单组处理完成：成功 ${succeeded} 张" in INDEX


def test_status_backend_keeps_manual_targets_locks_and_automatic_audit() -> None:
    assert "if target not in _MANUAL_ORDER_STATUS_TARGETS" in ORDERS
    assert "_lock_orders_for_production_transition" in ORDERS
    assert "_ensure_no_production_completion_facts" in ORDERS
    assert "_release_order_reservations" in ORDERS
    assert 'action_code="order.status_change"' in ORDERS
    audit = ORDERS.split('action_code="order.status_change"', 1)[1].split("db.commit()", 1)[0]
    for marker in ('"before": before', '"after": target', '"remark": remark'):
        assert marker in audit

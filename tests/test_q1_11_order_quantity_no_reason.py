from __future__ import annotations

import inspect
from pathlib import Path

from app.api.orders import OrderItemUpdate
from app.services.composite_bom_workflow import (
    append_component_demand_adjustment,
    append_order_quantity_adjustments,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_order_quantity_payload_has_no_free_text_adjustment_reason() -> None:
    assert "quantity_adjustment_reason" not in OrderItemUpdate.model_fields


def test_order_and_component_adjustments_use_system_action_labels() -> None:
    order_api = (ROOT / "app" / "api" / "orders.py").read_text(encoding="utf-8")
    component_service = inspect.getsource(append_component_demand_adjustment)
    assert 'reason="订单数量变更（系统记录）"' in order_api
    assert 'reason="订单组件需求变更（系统记录）"' in component_service
    assert "quantity_adjustment_reason" not in order_api


def test_quantity_adjustments_keep_cas_idempotency_and_history_guards() -> None:
    order_adjustment = inspect.getsource(append_order_quantity_adjustments)
    component_adjustment = inspect.getsource(append_component_demand_adjustment)
    assert "订单数量调整缺少幂等标识" in order_adjustment
    assert "调整后套数不能小于已处理套数" in order_adjustment
    assert "组件需求调整缺少幂等标识" in component_adjustment
    assert "if current != expected" in component_adjustment
    assert "组件需求已由其他操作" in component_adjustment


def test_component_quantity_ui_keeps_positive_integer_and_no_change_guards() -> None:
    assert "订单专用组件需求必须是正整数" in INDEX
    assert "本订单组件需求没有变化" in INDEX
    assert "expected_required_piece_quantity:current" in INDEX
    assert "idempotency_key:createIdempotencyKey()" in INDEX

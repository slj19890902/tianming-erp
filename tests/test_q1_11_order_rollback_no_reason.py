from __future__ import annotations

from pathlib import Path

from app.api.orders import WorkflowRollbackRequest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
ORDERS = (ROOT / "app" / "api" / "orders.py").read_text(encoding="utf-8")


def test_rollback_request_accepts_missing_blank_or_null_reason() -> None:
    assert WorkflowRollbackRequest().reason is None
    assert WorkflowRollbackRequest(reason="").reason == ""
    assert WorkflowRollbackRequest(reason=None).reason is None


def test_rollback_ui_uses_one_impact_confirmation_without_reason_prompt() -> None:
    assert "请输入原因" not in INDEX[INDEX.index("async rollbackOrderWorkflow"):INDEX.index("async openMobileEntry")]
    assert "送货、回单、对账、开票和收款记录" in INDEX
    assert 'axios.put(`/api/orders/${order.id}/rollback-workflow`,{})' in INDEX


def test_group_rollback_confirms_once_and_refreshes_once() -> None:
    assert "当前订单组的 ${orders.length} 张订单" in INDEX
    assert "{confirmed:true,reload:false,quiet:true}" in INDEX
    assert "订单组撤回完成：成功 ${succeeded} 张" in INDEX


def test_rollback_backend_keeps_all_dependency_and_transaction_guards() -> None:
    assert 'reason = (payload.reason or "").strip() or "订单流程撤回（系统记录）"' in ORDERS
    assert "_ensure_no_production_completion_facts" in ORDERS
    assert "_ensure_no_active_incoming_receipts" in ORDERS
    assert "该订单与其他订单共用送货单" in ORDERS
    assert "送货单已产生库存出库记录" in ORDERS
    assert "该订单与其他订单共用对账单" in ORDERS
    assert "_rollback_supplier_requisition_items" in ORDERS
    assert 'action_code="order.workflow_rollback"' in ORDERS
    assert "except Exception:\n        db.rollback()" in ORDERS

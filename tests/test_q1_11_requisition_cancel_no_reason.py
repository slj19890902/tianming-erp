from pathlib import Path

from app.api.requisition import CancelPayload


ROOT = Path(__file__).resolve().parents[1]
REQUISITION = (ROOT / "app" / "api" / "requisition.py").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_cancel_payload_accepts_missing_blank_and_legacy_reason() -> None:
    assert CancelPayload().reason is None
    assert CancelPayload(reason="").reason == ""
    assert CancelPayload(reason="供应商规格错误").reason == "供应商规格错误"


def test_cancel_requisition_frontend_has_one_confirmation_and_no_reason() -> None:
    start = INDEX.index("async cancelRequisition(row)")
    end = INDEX.index("normalizeReceiptResolution(line)", start)
    method = INDEX[start:end]
    assert method.count("confirm(") == 1
    assert "prompt(" not in method
    assert "{reason}" not in method
    assert "const targetId = Number(row?.item_id || 0)" in method
    assert "/api/requisition/items/${targetId}/cancel`, {})" in method
    assert "executeRequisitionVoidAction" in method


def test_trace_requisition_rollback_does_not_fabricate_reason() -> None:
    start = INDEX.index("async rollbackTraceEvent(event)")
    end = INDEX.index("traceEventTime(event)", start)
    method = INDEX[start:end]
    assert "/api/requisition/items/${this.orderTrace.item.id}/cancel`,{})" in method


def test_backend_keeps_status_inventory_scope_and_audit_gates() -> None:
    assert 'or "取消报料并退回待报料（系统记录）"' in REQUISITION
    assert "_require_order_item_customer_access" in REQUISITION
    assert 'item.material_status == "received"' in REQUISITION
    assert "IncomingReceiptItem.status == \"posted\"" in REQUISITION
    assert "release_active_finished_reservations_for_items" in REQUISITION
    assert "release_active_semi_reservations_for_items" in REQUISITION
    assert 'action="CANCEL_REQUISITION"' in REQUISITION
    assert '"before": before_requisition' in REQUISITION
    assert '"after": {' in REQUISITION
    assert "db.rollback()" in REQUISITION

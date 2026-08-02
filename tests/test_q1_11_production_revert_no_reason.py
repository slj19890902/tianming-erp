from pathlib import Path

from app.api.production import CompletionReversalRequest


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_API = (ROOT / "app" / "api" / "production.py").read_text(encoding="utf-8")
PRODUCTION_SERVICE = (ROOT / "app" / "services" / "production_workflow.py").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_reversal_payload_accepts_missing_blank_and_legacy_reason() -> None:
    assert CompletionReversalRequest().reason is None
    assert CompletionReversalRequest(reason="").reason == ""
    assert CompletionReversalRequest(reason="完工录错").reason == "完工录错"


def test_history_and_trace_use_one_confirmation_without_reason() -> None:
    start = INDEX.index("async revertProductionCompletion(row)")
    end = INDEX.index("async loadIncoming()", start)
    method = INDEX[start:end]
    assert method.count("confirm(") == 1
    assert "prompt(" not in method
    assert "生产完工历史回退（管理员一次确认）" not in method
    assert "await axios.post(`/api/production/completions/${row.id}/revert`, {})" in method
    trace_start = INDEX.index("async rollbackTraceEvent(event)")
    trace_end = INDEX.index("traceEventTime(event)", trace_start)
    trace = INDEX[trace_start:trace_end]
    assert "/api/production/completions/${event.source_id}/revert`,{})" in trace


def test_backend_keeps_scope_inventory_order_and_audit_gates() -> None:
    assert 'or "撤销生产确认（系统记录）"' in PRODUCTION_API
    assert 'or "撤销生产确认（系统记录）"' in PRODUCTION_SERVICE
    assert "Depends(admin_only)" in PRODUCTION_API
    assert "require_customer_access" in PRODUCTION_API
    assert "lock_order_rows_for_production_transition" in PRODUCTION_SERVICE
    assert "has_dispatched_delivery_facts" in PRODUCTION_SERVICE
    assert 'completion.completion_type == "primary"' in PRODUCTION_SERVICE
    assert "BomComponentDirectDeliveryAllocation" in PRODUCTION_SERVICE
    assert "_reverse_completion_finished_lot" in PRODUCTION_SERVICE
    assert "_reverse_completion_semi_consumption" in PRODUCTION_SERVICE
    assert 'action_code="production.completion.reverted"' in PRODUCTION_API
    assert "db.rollback()" in PRODUCTION_API

from pathlib import Path

from app.api.incoming import RevertRequest


ROOT = Path(__file__).resolve().parents[1]
INCOMING_API = (ROOT / "app" / "api" / "incoming.py").read_text(encoding="utf-8")
INCOMING_SERVICE = (ROOT / "app" / "services" / "incoming_receipts.py").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def test_revert_payload_accepts_missing_blank_and_legacy_reason() -> None:
    assert RevertRequest().reason is None
    assert RevertRequest(reason="").reason == ""
    assert RevertRequest(reason="数量录错").reason == "数量录错"


def test_desktop_and_mobile_use_one_confirmation_without_reason() -> None:
    start = INDEX.index("async revertIncoming(row)")
    end = INDEX.index("async loadIncomingHistory()", start)
    method = INDEX[start:end]
    assert method.count("confirm(") == 1
    assert "prompt(" not in method
    assert "row._revert_idempotency_key=row._revert_idempotency_key || createIdempotencyKey()" in method
    assert "await axios.put(url,{idempotency_key:row._revert_idempotency_key})" in method
    assert "操作人、时间和前后状态" in method
    assert "state.revertIdempotencyKeys.set(key, revertKey)" in MOBILE
    assert "body: JSON.stringify({idempotency_key: revertKey})" in MOBILE
    assert "来料实收历史回退（管理员一次确认）" not in MOBILE


def test_backend_keeps_scope_fact_inventory_and_audit_gates() -> None:
    assert 'or "撤回来料实收（系统记录）"' in INCOMING_API
    assert 'or "撤回来料实收（系统记录）"' in INCOMING_SERVICE
    assert "_preflight_receipt_item_customer_access" in INCOMING_API
    assert "lock_order_rows_for_production_transition" in INCOMING_SERVICE
    assert 'receipt_item.status != "posted"' in INCOMING_SERVICE
    assert "has_production_completion_facts" in INCOMING_SERVICE
    assert "latest.id != receipt_item.id" in INCOMING_SERVICE
    assert "_reverse_surplus_lot" in INCOMING_SERVICE
    assert 'action="REVERT_MATERIAL"' in INCOMING_SERVICE
    assert "db.rollback()" in INCOMING_API

from pathlib import Path


INDEX = (Path(__file__).parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_waiting_receipt_delivery_uses_controlled_edit_entry() -> None:
    assert "编辑待回单送货单" in INDEX
    assert "row.return_receipt_status==='cancelled'" in INDEX
    assert "@click=\"correctDeliveryActualDate(row)\">更正实际日期</button>" not in INDEX


def test_dispatched_edit_uses_revision_endpoint_and_reprint_prompt() -> None:
    assert "`/api/deliveries/${targetId}/revision`" in INDEX
    assert 'this.deliveryForm.editing_status === "dispatched"' in INDEX
    assert "payload.expected_version = Number(this.deliveryForm.version || 1)" in INDEX
    assert "送货单内容已修改，状态仍为已发货；请补打最新版" in INDEX
    assert "deliveryForm.editing_status==='pending'" in INDEX


def test_revision_editor_keeps_customer_locked_and_allows_line_adjustments() -> None:
    assert ':disabled="!!deliveryForm.editingId || deliveryCandidateLoading"' in INDEX
    assert 'editing_status:row.status || "pending"' in INDEX
    assert 'syncUnorderedDeliveryLineQuantity(line)' in INDEX
    assert 'Number(it.delivered_quantity || 0) + Number(pending?.deliverable_quantity || 0)' in INDEX

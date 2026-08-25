from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_SOURCE = (PROJECT_ROOT / "static" / "index.html").read_text(
    encoding="utf-8"
)
MOBILE_SOURCE = (PROJECT_ROOT / "static" / "mobile_erp.html").read_text(
    encoding="utf-8"
)
INCOMING_SOURCE = (PROJECT_ROOT / "static" / "incoming.html").read_text(
    encoding="utf-8"
)


def test_desktop_preserves_zero_and_receipt_auto_output_without_manual_controls() -> None:
    assert "row.completion_actionable === false" in DESKTOP_SOURCE
    assert "Number(row.available_material_input_quantity ?? 0)" in DESKTOP_SOURCE
    assert "? Number(row.actual_output_quantity ?? 0)" in DESKTOP_SOURCE
    assert "row.completion_block_message || '该任务由收料用途自动推进" in DESKTOP_SOURCE
    assert "成品由收料流水自动进入真实成品库位" in DESKTOP_SOURCE


def test_receipt_auto_delivery_uses_normal_delivery_picker_without_recompletion() -> None:
    assert '@click="openReceiptManagedDelivery(row)"' in DESKTOP_SOURCE
    quantity_start = DESKTOP_SOURCE.index("receiptManagedDeliveryQuantity(row) {")
    quantity_end = DESKTOP_SOURCE.index("async openReceiptManagedDelivery(row)", quantity_start)
    quantity_method = DESKTOP_SOURCE[quantity_start:quantity_end]
    assert "row?.delivery_actionable !== true" in quantity_method
    assert "row.delivery_ready_quantity" in quantity_method
    assert "row.order_reserved_quantity" not in quantity_method
    assert "row.actual_output_quantity" not in quantity_method
    start = DESKTOP_SOURCE.index("async openReceiptManagedDelivery(row)")
    end = DESKTOP_SOURCE.index("canConfirmProductionRow(row)", start)
    method = DESKTOP_SOURCE[start:end]
    assert 'axios.get("/api/deliveries/pending-items/search"' in method
    assert "order_item_id: orderItemId" in method
    assert "axios.post" not in method
    assert "/api/production/completion-batches" not in method
    assert "this.deliveryForm.saved_signature" not in method


def test_delivery_defaults_to_order_remaining_capped_by_real_deliverable() -> None:
    assert "deliveryDefaultQuantity(candidate)" in DESKTOP_SOURCE
    assert "Math.min(orderRemaining, deliverable)" in DESKTOP_SOURCE
    assert "delivered_quantity: this.deliveryDefaultQuantity(candidate)" in DESKTOP_SOURCE
    assert "line.delivered_quantity = this.deliveryDefaultQuantity(item) || null" in DESKTOP_SOURCE


def test_mobile_and_incoming_show_server_built_receipt_auto_progress() -> None:
    assert 'task.receipt_purpose_managed ? "已自动形成"' in MOBILE_SOURCE
    assert "task.completion_block_message" in MOBILE_SOURCE
    assert 'task.receipt_purpose_managed ? "已自动形成"' in INCOMING_SOURCE
    assert "task.completion_block_message" in INCOMING_SOURCE

"""Legacy reported rows retain the same frozen-history boundaries as new rows."""
from decimal import Decimal
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from test_phase11_requisition import requisition_app, _login


@pytest.mark.parametrize("block", ["received_partially", "cancelled", "force_closed"])
def test_legacy_reported_order_cannot_rewrite_received_or_closed_requirement(requisition_app, block):
    from app.models.order import OrderItem
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    app, factory = requisition_app
    with factory() as db:
        item = db.get(OrderItem, 1)
        item.requisition_status = "已报料"
        item.cardboard_len = 800
        item.cardboard_width = 200
        if block == "cancelled":
            item.order.status = "cancelled"
        elif block == "force_closed":
            item.is_force_closed = True
        else:
            receipt = IncomingReceipt(receipt_number="IR-LEGACY-PARTIAL", idempotency_key="legacy-partial",
                received_by=1, received_at=datetime(2026, 10, 9), status="posted")
            receipt.items.append(IncomingReceiptItem(order_id=item.order_id, order_item_id=item.id,
                planned_quantity=100, received_quantity=40, cumulative_received_quantity=40,
                variance_quantity=-60, variance_type="short", resolution_action="await_supplier",
                resolution_status="pending", status="posted"))
            db.add(receipt)
        db.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put("/api/requisition/items/1", json={"cardboard_len": 900,
            "cardboard_width": 250, "requisition_qty": 100, "inventory_deducted_qty": 0, "special_process": "一开一"})
        assert response.status_code == 409, response.text
    with factory() as db:
        item = db.get(OrderItem, 1)
        assert Decimal(item.cardboard_len) == Decimal(800)
        assert Decimal(item.cardboard_width) == Decimal(200)

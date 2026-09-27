from fastapi.testclient import TestClient
from sqlalchemy import select

from test_direct_external_finished import prepare, receive, routing_app, p1_40a_app, _p181_published_map_identity


def test_external_real_purchase_partial_receipt_and_reversal(routing_app):
    from app.models.order import OrderItem
    from app.models.external_packaging_purchase import ExternalPackagingReceiptReversal, ExternalPackagingPurchaseCancellation
    from app.services.pre_delivery_readiness import order_readiness, _external_inbound
    with TestClient(routing_app) as client:
        oid, pid, line = prepare(routing_app, client, ratio="2")
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            assert _external_inbound(db, item) == (1000, [])
        response = receive(client, pid, line, 400)
        assert response.status_code == 200, response.text
        rid = response.json()["receipt"]["id"]
        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            value = order_readiness(db, item)
            assert (value["finished_available"], value["effective_inbound"], value["pending_processing"]) == (200, 800, 0)
            # Append-only reversal/cancellation predicates are used by this read
            # projection. This isolated transaction is rolled back after testing.
            db.add(ExternalPackagingReceiptReversal(receipt_id=rid, reason="isolated regression",
                idempotency_key="readiness-reversal", request_fingerprint="a"*64))
            db.flush()
            assert _external_inbound(db, item) == (1000, [])
            db.add(ExternalPackagingPurchaseCancellation(purchase_order_id=pid, source="manual_purchase_cancel", reason="isolated regression"))
            db.flush()
            assert _external_inbound(db, item)[0] == 0

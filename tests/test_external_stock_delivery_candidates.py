"""Existing physical stock must be discoverable without a new purchase."""
import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_direct_external_finished import prepare, receive
from test_p1_40b_external_packaging_routing import routing_app, p1_40a_app, _order_payload_for, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def test_adopted_stock_candidate_guards_and_customer_scope(routing_app):
    from app.api.deliveries import router, _pending_query, _PendingDeliveryReadContext
    from app.models.access_control import UserPermissionOverride
    from app.models.order import Order, OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.warehouse_inventory import release_finished_reservation
    from tests.test_delivery_dispatch_commands import state

    routing_app.include_router(router, prefix="/api/deliveries")
    factory = routing_app.state.factory
    customer = routing_app.state.fixture["customer_a"]
    other_customer = routing_app.state.fixture["customer_b"]
    with TestClient(routing_app) as client:
        oid, purchase, line = prepare(routing_app, client, ratio="2", order_quantity=2000)
        received = receive(client, purchase, line, 1200)
        assert received.status_code == 200, received.text
        with factory() as db:
            original = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
            reservation = db.scalar(select(InventoryReservation).where(
                InventoryReservation.order_item_id == original.id))
            lot_id, product_id = reservation.inventory_lot_id, original.product_id
            release_finished_reservation(
                db, reservation_id=reservation.id, operator_id=reservation.reserved_by,
                release_reason="隔离验证采用库存", idempotency_key="candidate-source-release",
                allow_downstream=True,
            )
            db.commit()
            version = db.get(InventoryLot, lot_id).version
        payload = _order_payload_for(product_id, customer)
        payload["customer_po"] = "ISOLATED-EXTERNAL-STOCK-600"
        payload["items"][0].update(quantity=600, reservation_plan={"finished": [{
            "lot_id": lot_id, "expected_version": version,
            "requested_qty": 600, "confirmed": True,
        }]})
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        with factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == created.json()["id"]))
            item_id = item.id
            reservation = db.scalar(select(InventoryReservation).where(
                InventoryReservation.order_item_id == item_id))
            reservation_id = reservation.id
            assert reservation.reserved_stock_quantity == 1200
            assert reservation.requirement_quantity_denominator == 2
            before = state(factory)
            rows = list(db.execute(_pending_query(db=db, order_item_id=item_id)))
            assert len(rows) == 1
            assert _PendingDeliveryReadContext(db, rows).remaining_quantity(db, item) == 600
            assert state(factory) == before
            assert not list(db.execute(_pending_query(db=db, order_item_id=item_id,
                                                     customer_ids={other_customer})))

        # These isolated invalidations cannot acquire eligibility through the
        # new path. Roll back each one so the next case starts from valid facts.
        for invalidation in (
            "released", "cancelled", "missing_marker", "wrong_customer",
            "wrong_product", "wrong_ratio", "closed", "delivered", "void_order",
            "coated_board",
        ):
            with factory() as db:
                item = db.get(OrderItem, item_id)
                reservation = db.get(InventoryReservation, reservation_id)
                lot = db.get(InventoryLot, lot_id)
                if invalidation == "released":
                    release_finished_reservation(
                        db, reservation_id=reservation.id, operator_id=reservation.reserved_by,
                        release_reason="隔离验证取消抵扣", idempotency_key="candidate-release")
                elif invalidation == "cancelled":
                    reservation.status = "cancelled"
                elif invalidation == "missing_marker":
                    marker = json.loads(lot.finished_detail.physical_basis_json)
                    marker.pop("quantity_basis")
                    lot.finished_detail.physical_basis_json = json.dumps(marker)
                elif invalidation == "wrong_customer":
                    lot.finished_detail.owner_customer_id = other_customer
                elif invalidation == "wrong_product":
                    marker = json.loads(lot.finished_detail.physical_basis_json)
                    marker["quantity_basis"]["product_id"] = product_id + 10000
                    lot.finished_detail.physical_basis_json = json.dumps(marker)
                elif invalidation == "wrong_ratio":
                    reservation.requirement_quantity_denominator = 1
                elif invalidation == "closed":
                    item.is_force_closed = True
                elif invalidation == "delivered":
                    item.delivered_quantity = item.quantity
                elif invalidation == "void_order":
                    db.get(Order, item.order_id).status = "cancelled"
                elif invalidation == "coated_board":
                    item.external_packaging_category_code_snapshot = "coated_board"
                db.flush()
                assert not list(db.execute(_pending_query(db=db, order_item_id=item_id))), invalidation
                db.rollback()

        customers = client.get("/api/deliveries/pending-customer-options")
        assert customers.status_code == 200, customers.text
        assert any(row["customer_id"] == customer for row in customers.json()["items"])
        # Existing route permissions and selected-customer scopes still apply.
        with factory() as db:
            user = db.scalar(select(User).where(User.username == "p1-40b-scoped"))
            user.permission_overrides.extend([
                UserPermissionOverride(permission_code="deliveries.view", is_allowed=True),
                UserPermissionOverride(permission_code="deliveries.execute", is_allowed=True),
            ])
            db.commit()
        _login(client, "p1-40b-scoped")
        assert not client.get("/api/deliveries/pending_items").json()["items"]
        denied = client.get("/api/deliveries/selection-items", params={"customer_id": customer})
        assert denied.status_code == 403, denied.text

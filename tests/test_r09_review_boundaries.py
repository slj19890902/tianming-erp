"""Independent R09 review boundaries; all fixtures run under the audit sandbox."""
from decimal import Decimal

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_direct_external_finished import prepare, receive, routing_app, p1_40a_app
from test_p1_40b_external_packaging_routing import _order_payload_for
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def _backlog(factory, order_id: int, quantity: int = 1000) -> int:
    from app.models.delivery_backlog import DeliveryBacklog
    from app.models.order import Order, OrderItem

    with factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        order = db.get(Order, order_id)
        assert item is not None and order is not None
        row = DeliveryBacklog(
            customer_id=order.customer_id,
            product_id=item.product_id,
            order_item_id=item.id,
            stock_code=item.snapshot_product_code,
            product_name=item.snapshot_product_name,
            customer_order_no=order.customer_po,
            due_date=order.delivery_date,
            original_quantity=quantity,
            target_quantity=quantity,
            reason="R09 独立复审",
            created_by=1,
        )
        db.add(row)
        db.commit()
        return row.id


def _new_frozen_order_for_existing_product(app, client, *, product_id: int, customer_id: int):
    """Create the next order after a master-ratio change, then confirm its purchase."""
    payload = _order_payload_for(product_id, customer_id)
    payload["items"][0]["quantity"] = 1000
    created = client.post("/api/orders", json=payload)
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    preview = client.get(f"/api/orders/{order_id}/external-packaging-purchase")
    assert preview.status_code == 200, preview.text
    confirmed = client.post(
        f"/api/orders/{order_id}/external-packaging-purchase/confirm",
        json={
            "idempotency_key": f"r09-review-second-{order_id}",
            "lines": [
                {
                    "order_component_id": row["order_component_id"],
                    "candidate_id": row["default_candidate_id"],
                    "purchase_quantity": row["suggested_purchase_quantity"],
                }
                for row in preview.json()["items"]
            ],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    pending = client.get("/api/external-packaging-purchases/pending-receipts")
    assert pending.status_code == 200, pending.text
    purchase = pending.json()["purchase_orders"][0]
    return order_id, purchase["id"], purchase["items"][0]["purchase_item_id"]


def test_r09_backlog_uses_order_frozen_ratio_after_master_ratio_changes(routing_app, _p181_published_map_identity):
    from app.models.delivery_backlog import DeliveryBacklog
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.services.delivery_backlogs import describe

    with TestClient(routing_app) as client:
        order_id, purchase_id, line_id = prepare(routing_app, client, ratio="2")
        received = receive(client, purchase_id, line_id, 2000, key="r09-review-frozen")
        assert received.status_code == 200, received.text
    backlog_id = _backlog(routing_app.state.factory, order_id)
    with routing_app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        assert item is not None
        product = db.get(Product, item.product_id)
        assert product is not None
        expected_units = (
            item.sales_unit_snapshot,
            item.external_packaging_purchase_unit_snapshot,
        )
        product.external_packaging_default_purchase_quantity_basis = Decimal("7")
        db.commit()
        result = describe(db, db.get(DeliveryBacklog, backlog_id))

    assert (result["ready_quantity"], result["ready_physical_quantity"]) == (1000, 2000)
    assert (result["customer_unit"], result["physical_unit"]) == expected_units


def test_r09_odd_customer_suggestion_never_projects_impossible_physical_quantity(routing_app, _p181_published_map_identity):
    """2 customer units : 1 physical unit can only suggest even customer counts."""
    from app.models.delivery_backlog import DeliveryBacklog
    from app.models.order import OrderItem
    from app.models.user import User
    from app.services.delivery_backlogs import suggestions

    with TestClient(routing_app) as client:
        order_id, purchase_id, line_id = prepare(routing_app, client, ratio="0.5")
        received = receive(client, purchase_id, line_id, 500, key="r09-review-odd")
        assert received.status_code == 200, received.text
    backlog_id = _backlog(routing_app.state.factory, order_id)
    with routing_app.state.factory() as db:
        backlog = db.get(DeliveryBacklog, backlog_id)
        item = db.get(OrderItem, backlog.order_item_id)
        actor = db.scalar(select(User).where(User.username == "p1-40a-admin"))
        assert item is not None and actor is not None
        preview = suggestions(
            db,
            actor,
            customer_id=backlog.customer_id,
            product_id=item.product_id,
            quantity=1,
            customer_order_no=backlog.customer_order_no,
        )

    assert preview["requested_quantity"] == 1
    assert preview["unallocated_quantity"] == 1
    assert all(row["suggested_quantity"] == 0 for row in preview["items"])
    assert all(not row["candidate"]["inventory_sources"] for row in preview["items"])


def test_r09_backlog_customer_scope_blocks_list_and_suggest(
    routing_app, _p181_published_map_identity
):
    """A scoped user must neither see nor generate a different customer's backlog."""
    from app.models.order import OrderItem
    from app.models.user import User
    from app.services.delivery_backlogs import listing, suggestions

    with TestClient(routing_app) as client:
        order_id, purchase_id, line_id = prepare(routing_app, client, ratio="2")
        received = receive(client, purchase_id, line_id, 2000, key="r09-review-scope")
        assert received.status_code == 200, received.text
    _backlog(routing_app.state.factory, order_id)
    with routing_app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        actor = db.scalar(select(User).where(User.username == "p1-40b-scoped"))
        assert item is not None and actor is not None
        assert listing(db, actor)["items"] == []
        with pytest.raises(HTTPException) as denied:
            suggestions(
                db,
                actor,
                customer_id=routing_app.state.fixture["customer_a"],
                product_id=item.product_id,
                quantity=1000,
            )
    assert denied.value.status_code == 403


def test_r09_suggestions_keep_each_old_order_frozen_ratio_and_source_plan(
    routing_app, _p181_published_map_identity
):
    """One product can have old orders frozen with different physical ratios."""
    from fractions import Fraction

    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.user import User
    from app.services.delivery_backlogs import suggestions
    from app.services.delivery_quantities import order_basis, physical_for

    with TestClient(routing_app) as client:
        first_order_id, first_purchase_id, first_line_id = prepare(
            routing_app, client, ratio="2"
        )
        first_received = receive(
            client, first_purchase_id, first_line_id, 2000, key="r09-review-first-order"
        )
        assert first_received.status_code == 200, first_received.text
        with routing_app.state.factory() as db:
            first_item = db.scalar(
                select(OrderItem).where(OrderItem.order_id == first_order_id)
            )
            product = db.get(Product, first_item.product_id)
            assert product is not None
            product.external_packaging_default_purchase_quantity_basis = Decimal("0.5")
            db.commit()
            product_id = first_item.product_id
            customer_id = routing_app.state.fixture["customer_a"]
        second_order_id, second_purchase_id, second_line_id = _new_frozen_order_for_existing_product(
            routing_app, client, product_id=product_id, customer_id=customer_id
        )
        second_received = receive(
            client, second_purchase_id, second_line_id, 500, key="r09-review-second-order"
        )
        assert second_received.status_code == 200, second_received.text

    _backlog(routing_app.state.factory, first_order_id)
    _backlog(routing_app.state.factory, second_order_id)
    with routing_app.state.factory() as db:
        actor = db.scalar(select(User).where(User.username == "p1-40a-admin"))
        preview = suggestions(
            db,
            actor,
            customer_id=customer_id,
            product_id=product_id,
            quantity=1500,
        )
        items = {
            row["order_item_id"]: row
            for row in preview["items"]
            if row["suggested_quantity"]
        }
        assert preview["unallocated_quantity"] == 0
        assert len(items) == 2
        for item_id, row in items.items():
            order_item = db.get(OrderItem, item_id)
            basis = order_basis(order_item, customer_id)
            source_credit = sum(
                (
                    Fraction(source["requirement_numerator"], source["requirement_denominator"])
                    for source in row["candidate"]["inventory_sources"]
                ),
                Fraction(0),
            )
            assert source_credit == row["suggested_quantity"]
            assert sum(
                source["quantity_to_pick_stock"]
                for source in row["candidate"]["inventory_sources"]
            ) == physical_for(basis, row["suggested_quantity"])

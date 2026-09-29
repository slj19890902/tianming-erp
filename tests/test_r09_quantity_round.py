"""R09 backlog projections must keep the frozen customer/physical contract."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_direct_external_finished import prepare, receive, routing_app, p1_40a_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def _backlog(factory, order_id, quantity=1000):
    from app.models.delivery_backlog import DeliveryBacklog
    from app.models.order import Order, OrderItem

    with factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        order = db.get(Order, order_id)
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
            reason="隔离测试待补送",
            created_by=1,
        )
        db.add(row)
        db.commit()
        return row.id, item.id, order.customer_id


@pytest.mark.parametrize(
    ("ratio", "physical"),
    [("0.5", 500), ("2", 2000)],
)
def test_backlog_list_and_preview_use_customer_units(
    routing_app, ratio, physical
):
    from app.models.user import User
    from app.services.delivery_backlogs import listing, suggestions

    with TestClient(routing_app) as client:
        order_id, purchase_id, line_id = prepare(routing_app, client, ratio=ratio)
        posted = receive(
            client,
            purchase_id,
            line_id,
            physical,
            key=f"r09-ratio-{ratio}",
        )
        assert posted.status_code == 200, posted.text
    backlog_id, _, customer_id = _backlog(routing_app.state.factory, order_id)

    with routing_app.state.factory() as db:
        from app.models.order import OrderItem
        from app.services.delivery_quantities import order_basis
        actor = db.scalar(select(User).where(User.username == "p1-40a-admin"))
        row = listing(db, actor)["items"][0]
        assert row["id"] == backlog_id
        item = db.get(OrderItem, row["order_item_id"])
        basis = order_basis(item, customer_id)
        assert row["remaining_quantity"] == 1000
        assert row["ready_quantity"] == 1000
        assert row["ready_physical_quantity"] == physical
        assert row["customer_unit"] == basis["customer_unit"]
        assert row["physical_unit"] == basis["physical_unit"]

        preview = suggestions(
            db,
            actor,
            customer_id=customer_id,
            product_id=row["product_id"],
            quantity=1000,
            customer_order_no=row["customer_order_no"],
        )
        assert preview["unallocated_quantity"] == 0
        assert preview["items"][0]["suggested_quantity"] == 1000
        assert preview["items"][0]["candidate"]["deliverable_quantity"] == 1000


def test_backlog_exact_denominator_combines_lots_without_rounding(routing_app):
    from app.models.delivery_backlog import DeliveryBacklog
    from app.services.delivery_backlogs import describe

    with TestClient(routing_app) as client:
        order_id, purchase_id, line_id = prepare(routing_app, client, ratio="3")
        first = receive(client, purchase_id, line_id, 2, key="r09-third-1")
        second = receive(client, purchase_id, line_id, 4, key="r09-third-2")
        assert first.status_code == second.status_code == 200
    backlog_id, _, _ = _backlog(routing_app.state.factory, order_id, quantity=2)

    with routing_app.state.factory() as db:
        row = describe(db, db.get(DeliveryBacklog, backlog_id))
        assert row["ready_quantity"] == 2
        assert row["ready_physical_quantity"] == 6


def test_backlog_dispatch_cancel_and_print_keep_both_quantities(routing_app):
    from app.api.deliveries import router as deliveries_router
    from app.api.tianhua_pre_delivery import router as tianhua_router
    from app.models.delivery_backlog import DeliveryBacklog
    from app.models.delivery import DeliveryItem
    from app.services.delivery_backlogs import describe
    from app.services.delivery_quantities import decode

    routing_app.include_router(deliveries_router, prefix="/api/deliveries")
    routing_app.include_router(tianhua_router, prefix="/api/deliveries")
    with TestClient(routing_app) as client:
        order_id, purchase_id, line_id = prepare(routing_app, client, ratio="0.5")
        posted = receive(client, purchase_id, line_id, 500, key="r09-flow-stock")
        assert posted.status_code == 200, posted.text
        backlog_id, item_id, customer_id = _backlog(
            routing_app.state.factory, order_id
        )
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": customer_id,
                "delivery_date": date.today().isoformat(),
                "items": [{"order_item_id": item_id, "delivered_quantity": 400}],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

        printed = client.get(f"/api/deliveries/{delivery_id}/print")
        assert printed.status_code == 200, printed.text
        assert printed.json()["items"][0]["quantity"] == 400
        assert printed.json()["actual_goods_items"][0]["quantity"] == 200

        with routing_app.state.factory() as db:
            row = describe(db, db.get(DeliveryBacklog, backlog_id))
            assert (row["fulfilled_quantity"], row["remaining_quantity"]) == (400, 600)
            assert (row["ready_quantity"], row["ready_physical_quantity"]) == (600, 300)
            line = db.scalar(
                select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id)
            )
            assert decode(line.quantity_contract_json)["physical_quantity"] == 200

        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert client.put(f"/api/deliveries/{delivery_id}/cancel").status_code == 409
        with routing_app.state.factory() as db:
            row = describe(db, db.get(DeliveryBacklog, backlog_id))
            assert (row["fulfilled_quantity"], row["remaining_quantity"]) == (0, 1000)
            assert (row["ready_quantity"], row["ready_physical_quantity"]) == (1000, 500)

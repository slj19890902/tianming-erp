from __future__ import annotations

from test_n029_production_service import production_app

from app.api.warehouse import _mapped_live_production_task_ids
from app.models.order import Order, OrderItem


def test_map_pending_occupancy_uses_order_item_fulfillment_policy(
    production_app,
) -> None:
    _app, factory, ids = production_app
    cases = ids["cases"]
    with factory() as db:
        partial_order = db.get(Order, cases["direct"]["order"])
        partial_item = db.get(OrderItem, cases["direct"]["item"])
        closed_order = db.get(Order, cases["stock"]["order"])
        force_closed_item = db.get(OrderItem, cases["transfer"]["item"])
        fully_delivered_item = db.get(OrderItem, cases["idem"]["item"])
        active_order = db.get(Order, cases["atomic-a"]["order"])
        assert all(
            row is not None
            for row in (
                partial_order,
                partial_item,
                closed_order,
                force_closed_item,
                fully_delivered_item,
                active_order,
            )
        )

        partial_order.status = "partially_delivered"
        partial_item.delivered_quantity = 1
        closed_order.status = "closed"
        force_closed_item.is_force_closed = True
        fully_delivered_item.delivered_quantity = fully_delivered_item.quantity
        active_order.status = "pending_production"
        db.commit()

        live_ids = _mapped_live_production_task_ids(db)

    assert cases["direct"]["task"] in live_ids
    assert cases["atomic-a"]["task"] in live_ids
    assert cases["stock"]["task"] not in live_ids
    assert cases["transfer"]["task"] not in live_ids
    assert cases["idem"]["task"] not in live_ids

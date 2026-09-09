from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models.order import Order, OrderItem
from app.models.multilevel_bom import OrderBomGraph, OrderBomGraphProduct
from app.models.product_bom import SalesOrderItemBomComponent
from app.api.deliveries import _delivery_list_component_demands
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.multilevel_bom_delivery_page import project_page_graph_demands
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_compile import setup_liner


def inputs(db, items):
    snapshots = list(db.scalars(select(SalesOrderItemBomComponent).where(
        SalesOrderItemBomComponent.sales_order_item_id.in_([item.id for item in items]))))
    return dict(graphs={item.id: db.get(OrderBomGraph, item.id) for item in items},
        order_items={item.id: item for item in items},
        orders={item.order_id: db.get(Order, item.order_id) for item in items},
        snapshots=snapshots, demands=_delivery_list_component_demands(snapshots, {}))


def test_graph_page_has_one_identity_query_for_one_or_multiple_orders(context):
    db, actor, first, _ = context
    setup_liner(db, actor)
    freeze_master_order_bom(db, order_item_id=first.id, actor=actor)
    second = OrderItem(order_id=first.order_id, product_id=first.product_id,
        snapshot_product_name=first.snapshot_product_name,
        quantity=30, unit_price=Decimal("5"), subtotal=Decimal("150"))
    db.add(second)
    db.flush()
    freeze_master_order_bom(db, order_item_id=second.id, actor=actor)
    db.commit()
    for selected in ([first], [first, second]):
        payload = inputs(db, selected)
        queries = []
        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            queries.append(statement)
        event.listen(db.bind, "before_cursor_execute", capture)
        try:
            roots = project_page_graph_demands(db, **payload)
        finally:
            event.remove(db.bind, "before_cursor_execute", capture)
        assert len(queries) == 1
        assert set(roots) == {item.id for item in selected}
        for item in selected:
            assert {d.component_product_id for d in payload["demands"][item.id]} == {1, 2}
            assert all(d.required_piece_quantity == item.quantity for d in payload["demands"][item.id])


@pytest.mark.parametrize("damage", ["hash", "identity", "missing_snapshot", "missing_graph"])
def test_page_rejects_corrupt_graph_instead_of_flat_fallback(context, damage):
    db, actor, item, _ = context
    setup_liner(db, actor)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    payload = inputs(db, [item])
    if damage == "hash":
        payload["graphs"][item.id].content_hash = "0" * 64
    elif damage == "identity":
        db.get(OrderBomGraphProduct, (item.id, 2)).product_version += 1
        db.flush()
    elif damage == "missing_snapshot":
        payload["snapshots"].pop()
    else:
        payload["graphs"].clear()
    with pytest.raises(BomPlanError):
        project_page_graph_demands(db, **payload)

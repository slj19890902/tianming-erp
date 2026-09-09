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


def test_graph_page_has_fixed_identity_and_revision_queries_for_multiple_orders(context):
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
        assert len(queries) == 2  # one identity batch and one amendment batch
        assert set(roots) == {item.id for item in selected}
        for item in selected:
            assert {d.component_product_id for d in payload["demands"][item.id]} == {1, 2}
            assert all(d.required_piece_quantity == item.quantity for d in payload["demands"][item.id])
        from app.services.multilevel_bom_delivery_page import summary_graph_contracts
        queries.clear()
        event.listen(db.bind, "before_cursor_execute", capture)
        try:
            picks, summary_roots, excluded = summary_graph_contracts(db, [item.id for item in selected])
        finally:
            event.remove(db.bind, "before_cursor_execute", capture)
        assert len(queries) == 4  # three original batches plus all amendments
        assert summary_roots == roots
        assert len(picks) == len(selected) * 2 and len(excluded) == len(selected) * 2


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


def test_collapsed_component_delivery_counts_only_real_pick_nodes(context):
    from app.api.deliveries import _delivery_list_summary_context, _delivery_summary_response, _delivery_response
    from tests.test_composite_component_delivery_quantities import _delivery
    db, actor, item, _ = context
    setup_liner(db, actor)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    item.composite_fulfillment_mode_snapshot = "component_delivery"
    delivery, _ = _delivery(db, customer_id=136, order_item_id=item.id, number="GRAPH-SUMMARY", quantity=10)
    db.commit()
    detail = _delivery_response(db, delivery.id)
    summary = _delivery_summary_response(delivery.id, context=_delivery_list_summary_context(db, [delivery.id]))
    assert detail["total_actual_goods_quantity"] == 20
    assert summary["total_actual_goods_quantity"] == detail["total_actual_goods_quantity"]


@pytest.mark.parametrize("missing", ["graph", "snapshots"])
def test_collapsed_graph_missing_facts_do_not_become_legacy(context, missing):
    from sqlalchemy import delete
    from app.api.deliveries import _delivery_list_summary_context
    from tests.test_composite_component_delivery_quantities import _delivery
    db, actor, item, _ = context
    setup_liner(db, actor)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    item.composite_fulfillment_mode_snapshot = "component_delivery"
    delivery, _ = _delivery(db, customer_id=136, order_item_id=item.id, number="BAD-GRAPH-SUMMARY", quantity=10)
    db.commit()
    if missing == "graph":
        db.execute(delete(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id == item.id))
        db.execute(delete(OrderBomGraph).where(OrderBomGraph.order_item_id == item.id))
    else:
        db.execute(delete(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.sales_order_item_id == item.id))
    with pytest.raises(BomPlanError):
        _delivery_list_summary_context(db, [delivery.id])


@pytest.mark.parametrize("summary", [False, True])
def test_batch_delivery_rejects_corrupt_production_revision(context, summary):
    from app.services.multilevel_bom_production_versions import append_order_production_revision
    from app.services.multilevel_bom_delivery_page import summary_graph_contracts
    db, actor, item, _ = context
    setup_liner(db, actor)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    revision = append_order_production_revision(db, order_item_id=item.id,
        changes={"1": {"report_width_mm": 710}}, expected_revision=0, actor=actor)
    db.commit()
    # Isolated corruption injection: keep the old checksum to prove readers
    # cannot bypass the amendment chain simply because pick counts match.
    revision.document_json = revision.document_json.replace("710", "711")
    db.flush()
    with pytest.raises(BomPlanError, match="修订校验失败"):
        if summary:
            summary_graph_contracts(db, [item.id])
        else:
            project_page_graph_demands(db, **inputs(db, [item]))

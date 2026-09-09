import pytest
from sqlalchemy import event

from app.services.composite_bom_workflow import delivery_component_demands, delivery_component_required_quantities
from app.services.multilevel_bom_delivery_page import project_page_graph_demands, summary_graph_contracts
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_multilevel_bom_orders import context
from tests.test_bom_cutover_source_read import cutover_read_fixture
from tests.test_multilevel_bom_delivery_page import inputs


def test_current_delivery_demands_exclude_old_same_product(cutover_read_fixture):
    db, _, item, history, current, _ = cutover_read_fixture
    demands = delivery_component_demands(db, item.id)
    assert {d.component_product_id for d in demands} == {1, 2}
    assert len(demands) == 2
    assert history.id not in {d.snapshot_id for d in demands}
    assert {d.required_piece_quantity for d in demands} == {80}
    assert {d.delivered_before_cutover for d in demands} == {20}
    assert set(delivery_component_required_quantities(db, order_item_id=item.id, delivery_sets=1).values()) == {1}


def test_page_and_summary_share_current_window(cutover_read_fixture):
    from app.api.deliveries import _delivery_list_component_required_quantities
    db, _, item, history, _, _ = cutover_read_fixture
    payload = inputs(db, [item])
    queries = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        roots = project_page_graph_demands(db, **payload)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(queries) == 3  # identity + revision + cutover role batches
    demands = payload["demands"][item.id]
    assert len(demands) == 2 and all(d.required_piece_quantity == 80 for d in demands)
    assert history.id not in {d.snapshot_id for d in demands}
    required = _delivery_list_component_required_quantities({
        "component_demands_by_order_item": payload["demands"], "component_delivered_by_snapshot": {}},
        order_item=item, delivery_sets=1)
    assert set(required.values()) == {1}
    contracts = summary_graph_contracts(db, [item.id])
    assert contracts.roots == roots
    assert set(contracts.picks.values()) == {(1, 80)}
    assert contracts.history_ids == {history.id}
    assert history.id not in contracts.excluded  # history remains visible on old documents
    assert contracts.delivered_before == {item.id: 20}


def test_pending_delivery_actual_summary_uses_only_new_epoch(cutover_read_fixture):
    from app.api.deliveries import _delivery_list_summary_context, _delivery_summary_response, _delivery_response
    from tests.test_composite_component_delivery_quantities import _delivery
    db, _, item, _, _, _ = cutover_read_fixture
    item.composite_fulfillment_mode_snapshot = "component_delivery"
    delivery, _ = _delivery(db, customer_id=136, order_item_id=item.id, number="CUTOVER-NEXT", quantity=1)
    db.commit()
    detail = _delivery_response(db, delivery.id)
    summary = _delivery_summary_response(delivery.id, context=_delivery_list_summary_context(db, [delivery.id]))
    assert detail["total_actual_goods_quantity"] == 2  # one carton + one liner
    assert summary["total_actual_goods_quantity"] == 2


def historical_delivery_fixture(cutover_read_fixture, *, source=None):
    from datetime import datetime
    from app.models.production import ProductionTask, ProductionCompletionBatch, ProductionCompletion
    from app.models.product_bom import BomComponentDirectDeliveryAllocation
    from tests.test_composite_component_delivery_quantities import _delivery
    db, _, item, history, _, _ = cutover_read_fixture
    history = source if source is not None else history
    item.composite_fulfillment_mode_snapshot = "component_delivery"
    delivery, line = _delivery(db, customer_id=136, order_item_id=item.id, number="CUTOVER-OLD", quantity=20)
    delivery.status = "dispatched"
    task = ProductionTask(order_item_id=item.id, sales_order_item_bom_component_id=history.id,
        task_role="component_internal", status="completed", planned_quantity=20,
        finished_coverage_snapshot=0, ordered_quantity_snapshot=20, material_received_quantity=20,
        material_input_quantity=20, output_factor=1, version=1)
    batch = ProductionCompletionBatch(idempotency_key="cutover-history-fixture", request_hash="b"*64,
        item_count=1, completed_at=datetime.now())
    db.add_all([task, batch])
    db.flush()
    completion = ProductionCompletion(batch_id=batch.id, task_id=task.id, order_item_id=item.id,
        expected_version=1, quantity=20, order_reserved_quantity=20, direct_delivery_quantity=20,
        stock_quantity=0, surplus_finished_quantity=0, initial_disposition="direct",
        status="posted", completed_at=datetime.now())
    db.add(completion)
    db.flush()
    allocation = BomComponentDirectDeliveryAllocation(delivery_item_id=line.id,
        production_completion_id=completion.id, sales_order_item_bom_component_id=history.id,
        consumed_quantity=20, reversed_quantity=0, status="active")
    db.add(allocation)
    db.commit()
    return delivery, line, allocation


def test_historical_delivery_summary_keeps_old_source_allocations(cutover_read_fixture):
    from app.api.deliveries import _delivery_list_summary_context, _delivery_summary_response
    db, _, _, _, _, _ = cutover_read_fixture
    delivery, _, allocation = historical_delivery_fixture(cutover_read_fixture)
    summary = _delivery_summary_response(delivery.id, context=_delivery_list_summary_context(db, [delivery.id]))
    assert summary["total_actual_goods_quantity"] == 20
    assert allocation.consumed_quantity == 20 and allocation.reversed_quantity == 0


@pytest.mark.parametrize("reader", ["detail", "page", "summary"])
def test_delivery_rejects_corrupt_old_sources_even_when_excluded(cutover_read_fixture, reader):
    db, _, item, history, _, _ = cutover_read_fixture
    history.snapshot_component_production_notes = "changed history"
    db.commit()
    with pytest.raises(BomPlanError):
        if reader == "detail":
            delivery_component_demands(db, item.id)
        elif reader == "page":
            project_page_graph_demands(db, **inputs(db, [item]))
        else:
            summary_graph_contracts(db, [item.id])

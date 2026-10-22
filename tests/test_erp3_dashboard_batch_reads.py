"""Behavior and SQL guards for dashboard preflight reads on synthetic data."""
from datetime import datetime
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from tests.test_erp3_backend_read_paths import erp3_read_app
from tests.test_phase3_scale_reliability import _admin
from tests.test_semi_finished_order_reservation import (
    b1_app, add_semi_lot, login, post_order, order_item, semi_plan,
)


def _capture(engine, call):
    statements = []

    def record(_conn, _cursor, statement, _parameters, _ctx, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any(sql.startswith(("insert ", "update ", "delete ")) for sql in statements)
    return result, statements


def test_empty_dashboard_scope_skips_global_eligibility(erp3_read_app):
    from app.api.dashboard import _authoritative_dashboard_data, _workflow_projection_rows

    _, engine, _, _, _ = erp3_read_app
    with Session(engine) as db:
        data, statements = _capture(engine, lambda: _authoritative_dashboard_data(
            db, user=_admin(), visible_customer_ids=set(), statement_month="2026-10",
            as_of="2026-10-22T12:00:00+08:00", can_view_requisition=True,
            can_view_incoming=True, can_view_orders=True, can_view_deliveries=True,
            can_view_finance=True, show_reconciliation_reminder=True,
        ))
        assert all(rows == [] for rows in data["rows"].values())
        assert data["requisition_waiting"] == {"waiting_count": 0, "due_waiting_count": 0}
        assert statements == []
        rows, statements = _capture(engine, lambda: _workflow_projection_rows(
            db, visible_customer_ids=set()
        ))
        assert rows == [] and statements == []


def test_dashboard_workflow_loads_liner_eligibility_snapshot_in_batch(erp3_read_app):
    from app.api.dashboard import _workflow_projection_rows
    from app.models.order import OrderItem
    from app.models.product import Product

    _, engine, _, _, _ = erp3_read_app
    with Session(engine) as db:
        db.scalar(select(Product)).box_style = "衬板"
        expected_item_ids = set(db.scalars(select(OrderItem.id)))
        db.commit()
    with Session(engine) as db:
        rows, statements = _capture(engine, lambda: _workflow_projection_rows(
            db, visible_customer_ids=None
        ))
    assert {row["item_id"] for row in rows} == expected_item_ids
    assert not any(sql.startswith("select sales_order_items.composite_fulfillment_mode_snapshot ")
                   for sql in statements)


def test_liner_preflight_batches_candidates_with_small_bind_limit(erp3_read_app):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.services.liner_direct_delivery import _LinerListReadContext, liner_direct_coverage

    _, engine, _, _, _ = erp3_read_app
    with Session(engine) as db:
        product = db.scalar(select(Product))
        product.box_style = "衬板"
        liner_product_id = product.id
        ordinary = Product(customer_id=product.customer_id, product_code="ERP3-ORDINARY",
                           customer_material_code="ERP3-ORDINARY", product_name="Synthetic carton",
                           box_style="普通箱")
        db.add(ordinary)
        db.flush()
        db.execute(update(OrderItem).where(OrderItem.id % 2 == 0).values(product_id=ordinary.id))
        db.commit()
    with Session(engine) as db:
        items = list(db.scalars(select(OrderItem)))
        expected = {item.id: liner_direct_coverage(db, item) for item in items}
        connection = db.connection().connection.driver_connection
        original_limit = connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        # Requested but absent IDs prove batching without inventing business facts.
        absent = [SimpleNamespace(id=value, product_id=liner_product_id,
                                  composite_fulfillment_mode_snapshot=None)
                  for value in range(10_000, 11_600)]
        try:
            context, statements = _capture(engine, lambda: _LinerListReadContext(db, [*items, *absent]))
            actual, reused = _capture(engine, lambda: {
                item.id: liner_direct_coverage(db, item, _read_context=context) for item in items
            })
        finally:
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original_limit)
        assert actual == expected
        assert len(statements) == 10  # products + three bounded preflight reads per chunk
        assert reused == []  # these fixture lines have no semi requirements


def test_liner_list_matches_live_frozen_reservation_and_new_completion(b1_app):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask, ProductionCompletion, ProductionCompletionBatch
    from app.models.warehouse_inventory import InventoryLot
    from app.services.liner_direct_delivery import direct_liner_item_ids, liner_direct_coverage

    app, factory = b1_app
    lot_id, version = add_semi_lot(factory, quantity=12, key="erp3-liner-batch")
    with factory() as db:
        product = db.get(Product, 1)
        product.box_style = "衬板"
        product.crease_type = "净料"
        db.get(InventoryLot, lot_id).semi_finished_detail.crease_type = "净料"
        db.commit()
    with TestClient(app) as client:
        login(client)
        response = post_order(client, [order_item(1, 5, {
            "semi": [semi_plan(lot_id, version, 5)]
        })], "ERP3-LINER-BATCH")
    assert response.status_code == 201, response.text
    with factory() as db:
        item = db.scalar(select(OrderItem))
        task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == item.id))
        product = db.get(Product, item.product_id)

        def assert_current(expected):
            assert liner_direct_coverage(db, item) == expected
            assert direct_liner_item_ids(db) == ([item.id] if expected else [])

        assert_current(5)
        previous_print = task.print_content_snapshot
        task.print_content_snapshot = "需印客户图案"
        db.flush()
        assert_current(0)
        task.print_content_snapshot = previous_print
        product.box_style = "普通箱"
        db.flush()
        assert_current(0)
        product.box_style = "衬板"
        db.flush()
        assert_current(5)
        # Explicit synthetic completion history tests read eligibility only.
        batch = ProductionCompletionBatch(idempotency_key="erp3-liner-completion", request_hash="a" * 64,
            item_count=1, completed_at=datetime(2026, 10, 22))
        db.add(batch)
        db.flush()
        completion = ProductionCompletion(batch_id=batch.id, task_id=task.id, order_item_id=item.id,
            expected_version=1, quantity=5, initial_disposition="direct", status="posted",
            completed_at=datetime(2026, 10, 22))
        db.add(completion)
        db.flush()
        assert_current(0)
        completion.status = "reversed"
        db.flush()
        assert_current(5)

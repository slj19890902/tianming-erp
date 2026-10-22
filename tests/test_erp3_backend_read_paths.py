"""Synthetic read-path budgets; no production database or writes in measured calls.

ERP3_READ_EVIDENCE optionally saves same-fixture latency/SQL/payload evidence.
ERP3_READ_BASELINE compares the complete payloads with a pre-change capture.
The fixture is direct synthetic history, not proof of the write-side workflow.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, event, select, update
from sqlalchemy.orm import Session

from tests.test_phase3_scale_reliability import _admin, _phase3_scale_app
from tests.test_phase8_dashboard import _freeze_dashboard_business_date


@pytest.fixture()
def erp3_read_app(tmp_path, monkeypatch):
    from app.api import incoming, dashboard
    from app.models import Base
    from app.models.order import OrderItem
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.product import Product
    from app.models.product_drawing import ProductDrawing

    fixture = _phase3_scale_app(tmp_path, (100,))
    app, engine, customer_ids = next(fixture)
    _freeze_dashboard_business_date(monkeypatch, date(2026, 10, 22))
    with Session(engine) as db:
        product_id = db.scalar(select(Product.id))
        # Equal upload dates deliberately test the stable ID tie-breaker.
        drawings = [
            ProductDrawing(product_id=product_id, image_path=path,
                           thumbnail_path="synthetic-thumbnail.png",
                           uploaded_at=datetime(2026, 9, 1))
            for path in ("synthetic-old.pdf", "synthetic-current.pdf",
                         "private:drawing_originals/synthetic-print.pdf")
        ]
        db.add_all(drawings)
        first_received = db.scalar(select(OrderItem).join(IncomingReceiptItem,
            IncomingReceiptItem.order_item_id == OrderItem.id).order_by(OrderItem.id))
        first_received.drawing_file = "synthetic-frozen.pdf"
        frozen_item_id = first_received.id
        db.flush()
        current_drawing_id = drawings[1].id
        # Keep server-generated timestamps deterministic for complete baseline
        # equality; this writes only this test's synthetic fixture before timing.
        for table in Base.metadata.tables.values():
            values = {column.name: datetime(2026, 9, 1)
                      for column in table.columns
                      if column.name in {"created_at", "updated_at"}
                      and isinstance(column.type, DateTime)}
            if values:
                db.execute(update(table).values(**values))
        db.commit()
    app.include_router(dashboard.router, prefix="/api/dashboard")
    app.include_router(incoming.router, prefix="/api/incoming")
    app.dependency_overrides[dashboard.can_read] = _admin
    app.dependency_overrides[incoming.can_read] = _admin
    try:
        yield app, engine, customer_ids[100], frozen_item_id, current_drawing_id
    finally:
        fixture.close()


def _read(client, engine, path, **params):
    sql = []

    def record(_conn, _cursor, statement, parameters, _ctx, _many):
        sql.append((" ".join(statement.lower().split()), parameters))

    event.listen(engine, "before_cursor_execute", record)
    started = perf_counter()
    try:
        response = client.get(path, params=params)
    finally:
        elapsed_ms = (perf_counter() - started) * 1000
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert not any(statement.startswith(("insert ", "update ", "delete ")) for statement, _ in sql)
    return response.json(), sql, elapsed_ms


def _record_evidence(name, payload, sql, timings):
    output = os.getenv("ERP3_READ_EVIDENCE")
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        evidence = json.loads(target.read_text()) if target.exists() else {}
        evidence[name] = {
            "fixture": "100 synthetic orders; 1-3 items; posted receipts and partial deliveries",
            "measured_at": datetime.now().astimezone().isoformat(),
            "samples_ms": [round(value, 3) for value in timings],
            "median_ms": round(median(timings), 3),
            "select_count": sum(statement.startswith("select ") for statement, _ in sql),
            "sql": [statement for statement, _ in sql],
            "payload": payload,
        }
        target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    baseline = os.getenv("ERP3_READ_BASELINE")
    if baseline:
        assert payload == json.loads(Path(baseline).read_text())[name]["payload"]


def test_dashboard_keeps_authoritative_payload_without_discarded_hold_queries(erp3_read_app):
    app, engine, _, _, _ = erp3_read_app
    timings = []
    with TestClient(app) as client:
        for _ in range(5):
            payload, sql, elapsed = _read(client, engine, "/api/dashboard/overview")
            timings.append(elapsed)
    _record_evidence("dashboard", payload, sql, timings)
    assert payload["workbench"]["tasks"]
    assert payload["kpi"] and payload["business_status_counts"]
    # Eligibility and the authoritative summary need three hold queries. Legacy
    # cards previously issued another three queries for values never returned.
    hold_sql = [statement for statement, _ in sql if "from requisition_holds" in statement]
    assert len(hold_sql) == 3


def test_order_list_registry_is_limited_to_visible_page_and_keeps_numbers(erp3_read_app):
    app, engine, customer_id, _, _ = erp3_read_app
    timings = []
    with TestClient(app) as client:
        for _ in range(5):
            payload, sql, elapsed = _read(client, engine, "/api/orders", scope="all",
                detail_level="summary", include_unfinished_total="false",
                customer_id=customer_id, page=2, page_size=7)
            timings.append(elapsed)
    _record_evidence("orders", payload, sql, timings)
    assert payload["total"] == 100
    assert len(payload["items"]) == 7
    assert all(row["order_number"] == row["display_order_number"] for row in payload["items"])
    registry_reads = [(statement, parameters) for statement, parameters in sql
                      if statement.startswith("select sales_orders.id, sales_orders.order_number from sales_orders")]
    assert len(registry_reads) == 1
    statement, parameters = registry_reads[0]
    assert "where sales_orders.id in (" in statement
    assert set(parameters) == {row["id"] for row in payload["items"]}


def test_received_history_batches_drawings_and_preserves_frozen_engineering_choice(erp3_read_app):
    app, engine, _, frozen_item_id, current_drawing_id = erp3_read_app
    timings = []
    with TestClient(app) as client:
        for _ in range(5):
            payload, sql, elapsed = _read(client, engine, "/api/incoming/history", page_size=100)
            timings.append(elapsed)
    _record_evidence("incoming_history", payload, sql, timings)
    facts = [row for row in payload["items"] if row.get("receipt_item_id")]
    assert len(facts) > 20
    frozen = next(row for row in facts if row["order_item_id"] == frozen_item_id)
    assert frozen["drawing_path"].startswith(f"/api/orders/items/{frozen_item_id}/drawing/")
    assert all(row["drawing_path"].startswith(f"/api/master/products/drawings/{current_drawing_id}/")
               for row in facts if row["order_item_id"] != frozen_item_id)
    drawing_reads = [statement for statement, _ in sql if "from product_drawings" in statement]
    # One batch for posted facts and at most one for compatibility rows.
    assert len(drawing_reads) <= 2
    assert not any(statement == "select sales_orders.id, sales_orders.order_number from sales_orders"
                   for statement, _ in sql)


def test_scoped_history_does_not_load_other_customer_fallback_drawings(erp3_read_app, monkeypatch):
    from app.api import incoming

    app, engine, _, _, _ = erp3_read_app
    monkeypatch.setattr(incoming, "_visible_customer_ids", lambda _user, _db: set())
    with TestClient(app) as client:
        payload, sql, _ = _read(client, engine, "/api/incoming/history", page_size=100)
    assert payload["items"] == []
    assert not any("from product_drawings" in statement for statement, _ in sql)


@pytest.mark.parametrize("only_print_artwork", [False, True])
def test_history_fallback_treats_whitespace_as_empty_and_never_uses_print_artwork(
    erp3_read_app, only_print_artwork
):
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, engine, _, frozen_item_id, current_drawing_id = erp3_read_app
    with Session(engine) as db:
        db.execute(update(OrderItem).where(OrderItem.id != frozen_item_id)
                   .values(drawing_file=" \t\n"))
        if only_print_artwork:
            for drawing in db.scalars(select(ProductDrawing)):
                drawing.image_path = f"private:drawing_originals/{drawing.id}.pdf"
        db.commit()
    with TestClient(app) as client:
        payload, _, _ = _read(client, engine, "/api/incoming/history", page_size=100)
    fallback_rows = [row for row in payload["items"]
                     if row.get("receipt_item_id") and row["order_item_id"] != frozen_item_id]
    assert fallback_rows
    if only_print_artwork:
        assert all(row["drawing_path"] is None for row in fallback_rows)
    else:
        assert all(row["drawing_path"].startswith(f"/api/master/products/drawings/{current_drawing_id}/")
                   for row in fallback_rows)


def test_history_lookup_batches_respect_small_sqlite_bind_limit(erp3_read_app):
    from app.api.incoming import _latest_engineering_drawings
    from app.models.order import Order
    from app.models.product import Product
    from app.services.order_number_display import build_display_registry_for_order_ids

    _, engine, _, _, current_drawing_id = erp3_read_app
    with Session(engine) as db:
        expected_numbers = dict(db.execute(select(Order.id, Order.order_number)).all())
        product_id = db.scalar(select(Product.id))
        connection = db.connection().connection.driver_connection
        original_limit = connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        try:
            registry = build_display_registry_for_order_ids(
                db, [*expected_numbers, *range(10_000, 10_900), *expected_numbers]
            )
            assert registry.by_order_id == expected_numbers
            drawings = _latest_engineering_drawings(db, {product_id, *range(10_000, 11_600)})
            assert {key: value.id for key, value in drawings.items()} == {
                product_id: current_drawing_id
            }
            assert build_display_registry_for_order_ids(db, set()).by_order_id == {}
            assert _latest_engineering_drawings(db, set()) == {}
        finally:
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original_limit)
